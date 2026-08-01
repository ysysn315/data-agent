"""Text-to-SQL 执行准确率评估入口（roadmap P1-2）。

    python -m evals.text2sql.run_execution_eval [--limit N] [--db PATH] [--model NAME]

一句话流程：**M-Schema + sql-generation 技能正文 → LLM 生成 SQL → sql_guard 校验 →
执行 → 与 golden 结果集按 execution accuracy 对比 → 出报告**。这正是把项目里
「Skills 即提示词模板」「M-Schema」「sqlglot 校验」三块能力串起来量化其端到端准确率。

依赖说明：
- 需要真实 LLM（走 app.core.llm.LLMFactory，任意 OpenAI 兼容端点），请先配好 .env
  的 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL。
- 需要演示库存在（默认 settings.sqlite_db_path，即 ./data/ecommerce.db）；
  没有就先跑 `python scripts/import_ecommerce.py --synthetic`。
- 生成 SQL 的校验走 sql_guard.validate_sql，**依赖 PR #5（fix/sql-guard-select-alias）
  合并**，否则 `ORDER BY 别名` 类查询会被误判校验失败而拉低分数（离线测试不受影响，
  它用原生 sqlite3 执行 golden，见 tests/test_text2sql_eval.py）。

本模块只做"编排 + IO"，纯粹的结果集归一化/对比/聚合逻辑都在 common.py（可独立测试）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from app.agents.tools.sql_guard import validate_sql
from app.core.llm import LLMFactory
from app.core.settings import settings
from app.skills.models import SkillContent
from app.text2sql.m_schema import generate_m_schema
from evals.text2sql.common import build_report, compare_result_sets, golden_has_order_by

# 路径锚定到 evals/text2sql/ 目录，脚本可在任意 cwd 下运行
_HERE = Path(__file__).resolve().parent
DATASET_PATH = _HERE / "dataset.json"
REPORTS_DIR = _HERE / "reports"
SKILL_MD_PATH = _HERE.parent.parent / "app" / "skills" / "buildin" / "sql-generation" / "SKILL.md"


def load_dataset(path: Path = DATASET_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_skill_body(path: Path = SKILL_MD_PATH) -> str:
    """读取 sql-generation 技能正文（剥离 frontmatter），即分层提示词本体。"""
    raw = path.read_text(encoding="utf-8")
    return SkillContent.parse(raw).body.strip()


def fetch_schema(db_path: str) -> dict[str, list[str]]:
    """{表名: [列名, ...]}，供 validate_sql 做表/列存在性校验。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        schema: dict[str, list[str]] = {}
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
        for (name,) in rows:
            if name.startswith("sqlite_"):
                continue
            cols = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            schema[name] = [c[1] for c in cols]
        return schema
    finally:
        conn.close()


def run_query(db_path: str, sql: str) -> list[tuple]:
    """只读执行一条 SQL，返回行列表。异常向上抛，由调用方归类为该例失败。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def build_prompt(skill_body: str, m_schema: str, question: str) -> list:
    """组装 system（技能正文 + M-Schema）+ human（问题）消息。

    评估阶段模型拿不到 schema_search 工具（不在 Agent 循环里），因此把整库 M-Schema
    直接注入 system —— demo 表少，全量注入等价于"检索已命中全部表"。要求模型只输出
    一条 SQL，便于抽取。
    """
    system = (
        f"{skill_body}\n\n"
        f"## 当前数据库结构（M-Schema）\n\n{m_schema}\n\n"
        f"## 输出要求\n\n"
        f"只输出一条 SQLite SELECT 语句，可用 ```sql 代码块包裹，"
        f"不要输出任何解释文字。"
    )
    return [SystemMessage(content=system), HumanMessage(content=question)]


def extract_sql(text: str) -> str:
    """从模型回复里抽出 SQL：优先取 ```sql 代码块，否则从首个 SELECT/WITH 起截取。"""
    if not text:
        return ""
    t = text.strip()
    # 1. 优先 ```sql ... ``` 或 ``` ... ``` 代码块
    if "```" in t:
        parts = t.split("```")
        # 奇数下标是代码块内容
        for i in range(1, len(parts), 2):
            block = parts[i]
            if block.lower().startswith("sql"):
                block = block[3:]
            block = block.strip()
            if block:
                t = block
                break
    # 2. 从首个 SELECT / WITH 关键词起截取，丢弃前置说明
    lowered = t.lower()
    idx = min(
        (p for p in (lowered.find("select"), lowered.find("with")) if p >= 0),
        default=-1,
    )
    if idx > 0:
        t = t[idx:]
    return t.strip().rstrip(";").strip()


def evaluate_case(
    case: dict,
    db_path: str,
    schema: dict[str, list[str]],
    skill_body: str,
    m_schema: str,
    llm,
) -> dict:
    """跑单个用例，返回可进 build_report 的结果字典。"""
    result = {
        "id": case["id"],
        "question": case["question"],
        "tags": case.get("tags", []),
        "golden_sql": case["golden_sql"],
        "pred_sql": None,
        "correct": False,
        "error": None,
    }
    try:
        # 1. LLM 生成
        messages = build_prompt(skill_body, m_schema, case["question"])
        raw = llm.invoke(messages).content
        pred_sql = extract_sql(raw)
        result["pred_sql"] = pred_sql
        if not pred_sql:
            result["error"] = "模型未产出可解析的 SQL"
            return result

        # 2. sql_guard 校验（表/列存在性、只读、自动 LIMIT）
        guard = validate_sql(pred_sql, schema=schema, default_limit=1000)
        if not guard.ok:
            result["error"] = f"校验失败: {guard.error}"
            return result

        # 3. 执行预测 SQL 与 golden SQL
        pred_rows = run_query(db_path, guard.fixed_sql)
        golden_rows = run_query(db_path, case["golden_sql"])

        # 4. execution accuracy 对比（行序敏感性由 golden 是否含 ORDER BY 决定）
        order_sensitive = golden_has_order_by(case["golden_sql"])
        result["correct"] = compare_result_sets(golden_rows, pred_rows, order_sensitive=order_sensitive)
    except Exception as e:  # noqa: BLE001 —— 单例失败不该中断整轮评估
        result["error"] = f"执行异常: {e}"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Text-to-SQL 执行准确率评估")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个用例（抽样）")
    parser.add_argument("--db", default=settings.sqlite_db_path, help="SQLite 演示库路径")
    parser.add_argument("--model", default=None, help="覆盖 LLM 模型名（默认取配置）")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"演示库不存在: {args.db}\n请先运行: python scripts/import_ecommerce.py --synthetic --db {args.db}")
        return 1

    dataset = load_dataset()
    if args.limit is not None:
        dataset = dataset[: args.limit]

    skill_body = read_skill_body()
    m_schema = generate_m_schema(args.db)
    schema = fetch_schema(args.db)
    llm = LLMFactory.create_llm(model=args.model, temperature=0.0, streaming=False)

    logger.info(f"开始评估：{len(dataset)} 个用例，库={args.db}")
    started = time.time()
    case_results = []
    for i, case in enumerate(dataset, start=1):
        r = evaluate_case(case, args.db, schema, skill_body, m_schema, llm)
        flag = "✓" if r["correct"] else "✗"
        logger.info(f"[{i}/{len(dataset)}] {flag} {r['id']} {r.get('error') or ''}")
        case_results.append(r)

    report = build_report(case_results)
    report["meta"] = {
        "db": args.db,
        "model": args.model or settings.llm_model,
        "num_cases": len(dataset),
        "elapsed_sec": round(time.time() - started, 1),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / "execution_latest.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n报告已保存: {out}")
    print(
        f"总体执行准确率: {report['summary']['accuracy']:.2%} "
        f"({report['summary']['correct']}/{report['summary']['total']})"
    )
    print("按标签:")
    for tag, b in report["by_tag"].items():
        print(f"  {tag:<10} {b['accuracy']:.2%} ({b['correct']}/{b['total']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
