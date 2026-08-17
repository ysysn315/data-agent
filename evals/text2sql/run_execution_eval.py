"""Text-to-SQL 执行准确率评估入口（roadmap P1-2）。

    python -m evals.text2sql.run_execution_eval [--limit N] [--db PATH] [--model NAME]
        [--tag TAG] [--difficulty LEVEL] [--no-skill]
        [--schema-mode m-schema|columns] [--output REPORT.json]

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
import hashlib
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


def select_cases(
    dataset: list[dict],
    tags: list[str] | None = None,
    difficulties: list[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """按标签交集、难度并集筛选；limit 最后生效，保证实验口径可预测。"""
    selected = dataset
    if tags:
        requested_tags = set(tags)
        selected = [case for case in selected if requested_tags <= set(case.get("tags", []))]
    if difficulties:
        requested_levels = set(difficulties)
        selected = [case for case in selected if case.get("difficulty") in requested_levels]
    if limit is not None:
        selected = selected[:limit]
    return selected


def dataset_fingerprint(dataset: list[dict]) -> str:
    """为本轮实际用例生成稳定指纹，避免同 ID 但题面/golden 已变时误做横向比较。"""
    comparable = [
        {
            "id": case["id"],
            "question": case["question"],
            "golden_sql": case["golden_sql"],
            "difficulty": case.get("difficulty"),
            "tags": case.get("tags", []),
        }
        for case in dataset
    ]
    payload = json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def build_prompt(skill_body: str, schema_context: str, question: str) -> list:
    """组装 system（可选技能正文 + Schema 上下文）+ human（问题）消息。

    评估阶段模型拿不到 schema_search 工具（不在 Agent 循环里），因此把整库 Schema
    直接注入 system；默认是带业务注释的 M-Schema，消融时可换成纯表/列/类型。demo 表少，
    全量注入等价于"检索已命中全部表"。要求模型只输出一条 SQL，便于抽取。
    """
    blocks = []
    if skill_body:
        blocks.append(skill_body)
    blocks.extend(
        [
            f"## 当前数据库结构\n\n{schema_context}",
            "## 输出要求\n\n只输出一条 SQLite SELECT 语句，可用 ```sql 代码块包裹，不要输出任何解释文字。",
        ]
    )
    system = "\n\n".join(blocks)
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
        "difficulty": case.get("difficulty"),
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
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="每例之间的间隔秒数（对低 RPM 配额的模型限速，避免 429 污染结果）",
    )
    parser.add_argument("--tag", action="append", default=[], help="只跑含该标签的题；可重复指定")
    parser.add_argument(
        "--difficulty",
        action="append",
        choices=("easy", "medium", "hard"),
        default=[],
        help="只跑指定难度；可重复指定",
    )
    parser.add_argument("--no-skill", action="store_true", help="不注入 sql-generation 技能正文，做消融对比")
    parser.add_argument(
        "--schema-mode",
        choices=("m-schema", "columns"),
        default="m-schema",
        help="m-schema 注入业务注释；columns 仅注入表、列和类型",
    )
    parser.add_argument("--output", type=Path, default=None, help="报告输出路径（默认覆盖 execution_latest.json）")
    parser.add_argument("--run-name", default=None, help="写入报告元信息的实验名称")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"演示库不存在: {args.db}\n请先运行: python scripts/import_ecommerce.py --synthetic --db {args.db}")
        return 1

    full_dataset = load_dataset()
    dataset = select_cases(full_dataset, tags=args.tag, difficulties=args.difficulty, limit=args.limit)
    if not dataset:
        print("没有匹配当前标签/难度筛选条件的评测用例")
        return 2

    skill_body = "" if args.no_skill else read_skill_body()
    schema_context = generate_m_schema(args.db, comments={} if args.schema_mode == "columns" else None)
    schema = fetch_schema(args.db)
    llm = LLMFactory.create_llm(model=args.model, temperature=0.0, streaming=False)

    logger.info(f"开始评估：{len(dataset)} 个用例，库={args.db}")
    started = time.time()
    case_results = []
    for i, case in enumerate(dataset, start=1):
        # 限流退避：429 属于"还没测到"，重试拿到真实结果（最多 5 次，指数退避）
        for attempt in range(5):
            r = evaluate_case(case, args.db, schema, skill_body, schema_context, llm)
            if "429" not in str(r.get("error") or ""):
                break
            wait = min(30, 5 * (attempt + 1))
            logger.warning(f"[{i}/{len(dataset)}] {r['id']} 触发限流，等 {wait}s 重试（第 {attempt + 1}/5 次）")
            time.sleep(wait)
        flag = "✓" if r["correct"] else "✗"
        logger.info(f"[{i}/{len(dataset)}] {flag} {r['id']} {r.get('error') or ''}")
        case_results.append(r)
        if args.interval:
            time.sleep(args.interval)

    report = build_report(case_results)
    report["meta"] = {
        "db": args.db,
        "model": args.model or settings.llm_model,
        "num_cases": len(dataset),
        "dataset_total_cases": len(full_dataset),
        "dataset_fingerprint": dataset_fingerprint(dataset),
        "elapsed_sec": round(time.time() - started, 1),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_name": args.run_name,
        "skill_enabled": not args.no_skill,
        "schema_mode": args.schema_mode,
        "filters": {"tags": args.tag, "difficulties": args.difficulty},
    }

    out = args.output or (REPORTS_DIR / "execution_latest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n报告已保存: {out}")
    print(
        f"总体执行准确率: {report['summary']['accuracy']:.2%} "
        f"({report['summary']['correct']}/{report['summary']['total']})"
    )
    print("按标签:")
    for tag, b in report["by_tag"].items():
        print(f"  {tag:<10} {b['accuracy']:.2%} ({b['correct']}/{b['total']})")
    print("按难度:")
    for level, b in report["by_difficulty"].items():
        print(f"  {level:<10} {b['accuracy']:.2%} ({b['correct']}/{b['total']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
