"""评测失败 case 导入候选示例库（知识回流闭环的评测侧入口）。

    python -m evals.text2sql.export_failures [--report PATH] [--datasource-id N] [--dry-run]

一句话流程：读评测报告（默认 execution_latest.json）里 correct=False 的 case，
把 (question, golden_sql) 以候选示例（verified=False, source='eval'）写入示例库，
人工在知识管理页转正后参与 few-shot 注入。

沉淀内容取舍：golden_sql 是人工标注的正确映射，正是示例库的知识本体；模型失败恰好
说明该 question 的 few-shot 缺失。pred_sql/error 不做示例（错误 SQL 进 prompt 有污染
风险），进 meta 作错误模式标注，供审核时并排查看"模型当时怎么错的"。

为什么是 CLI 直写 DB 而非 HTTP API：这是离线运营动作（评测后顺手导入），加 API
反而要背鉴权与上传协议；source='eval' 也因此不开放给 HTTP 白名单（manual|chat）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from app.text2sql.examples import ExampleStore

_HERE = Path(__file__).resolve().parent
DEFAULT_REPORT = _HERE / "reports" / "execution_latest.json"


def load_failed_cases(report_path: Path) -> list[dict]:
    """读报告并过滤失败 case（缺 golden_sql 的跳过——没有正确答案可沉淀）。"""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cases = report.get("cases", [])
    failed = [c for c in cases if not c.get("correct") and c.get("question") and c.get("golden_sql")]
    return failed


def build_candidate(case: dict, report_path: Path, datasource_id: int | None, workspace_id: int = 0) -> dict:
    """失败 case → 候选示例字段（meta 保留错误模式标注与报告出处）。"""
    return {
        "question": case["question"],
        "sql": case["golden_sql"],
        "verified": False,
        "datasource_id": datasource_id,
        "workspace_id": workspace_id,
        "source": "eval",
        "meta": {
            "case_id": case.get("id"),
            "tags": case.get("tags", []),
            "pred_sql": case.get("pred_sql"),
            "error": case.get("error"),
            "report": report_path.name,
        },
    }


def _same_scope(rec: dict, candidate: dict) -> bool:
    """作用域相等：平台数据源按 datasource_id；演示作用域按 (NULL, workspace_id)。"""
    if candidate.get("datasource_id") is not None:
        return rec.get("datasource_id") == candidate.get("datasource_id")
    return rec.get("datasource_id") is None and int(rec.get("workspace_id") or 0) == int(
        candidate.get("workspace_id") or 0
    )


def should_skip(store: "ExampleStore | None", candidate: dict) -> bool:
    """跳过规则：同作用域（datasource + workspace）同问题已存在已验证示例 → 不降级已有知识。

    store 传 None 时恒不跳过（无库可查），便于纯函数测试。
    """
    if store is None:
        return False
    existing = [r for r in store.list() if r["question"] == candidate["question"] and _same_scope(r, candidate)]
    return any(r.get("verified") for r in existing)


def main() -> None:
    parser = argparse.ArgumentParser(description="评测失败 case 导入候选 SQL 示例")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="评测报告 JSON 路径")
    parser.add_argument("--datasource-id", type=int, default=None, help="归属数据源（默认演示作用域）")
    parser.add_argument(
        "--workspace-id",
        type=int,
        default=0,
        help="归属 workspace（默认 0=演示；演示作用域的候选按 workspace 隔离）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将导入的内容，不写库")
    args = parser.parse_args()

    failed = load_failed_cases(args.report)
    if not failed:
        logger.info(f"报告 {args.report} 无失败 case（或缺 golden_sql），无需导入")
        return

    candidates = [build_candidate(c, args.report, args.datasource_id, args.workspace_id) for c in failed]

    if args.dry_run:
        for cand in candidates:
            err = cand["meta"].get("error") or cand["meta"].get("pred_sql")
            print(f"[候选] {cand['question']}\n  golden: {cand['sql']}\n  错误: {err}")
        print(f"\n共 {len(candidates)} 条候选（dry-run，未写库）")
        return

    # 与 dependencies.get_example_store 同款装配（DB 版直写；不 import 单例函数以免牵动 agent 全家桶）
    from app.core.settings import settings
    from app.db import ensure_initialized, get_sessionmaker, run_sync
    from app.db.repositories import SQLExampleRepository
    from app.text2sql.examples import ExampleStore

    ensure_initialized()
    store = ExampleStore(
        Path(settings.save_dir) / "sql_examples.json",
        repo=SQLExampleRepository(get_sessionmaker()),
        runner=run_sync,
    )

    imported = skipped = 0
    for cand in candidates:
        # 同作用域同问题已是已验证知识 → 跳过（不降级已有知识），否则按 (question, scope) 覆盖幂等导入
        if should_skip(store, cand):
            skipped += 1
            continue
        store.add(**cand)
        imported += 1

    logger.info(f"评测失败导入候选示例完成: 导入 {imported} 条, 跳过 {skipped} 条（已存在已验证同题示例）")
    print(f"导入 {imported} 条候选示例（verified=False），跳过 {skipped} 条；请到知识管理页转正")


if __name__ == "__main__":
    main()
