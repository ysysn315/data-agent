"""评测回流工具测试：export_failures 的候选构建/跳过规则 + compare_reports 的对比输出。

export_failures 的 main 走 settings+DB 装配（集成动作），这里测纯函数层
（load_failed_cases / build_candidate）与通过 ExampleStore（JSON 版）验证跳过规则；
compare_reports 全纯函数直测。
"""

from __future__ import annotations

import json

from app.text2sql.examples import ExampleStore
from evals.text2sql.compare_reports import compare
from evals.text2sql.export_failures import build_candidate, load_failed_cases, should_skip


def _write_report(path, cases):
    path.write_text(
        json.dumps(
            {
                "summary": {"total": len(cases), "correct": sum(c["correct"] for c in cases), "accuracy": 0.5},
                "by_tag": {},
                "cases": cases,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_load_failed_cases_filters_and_requires_golden(tmp_path):
    """只取失败 case，且必须有 golden_sql（没有正确答案无从沉淀）。"""
    report = tmp_path / "r.json"
    _write_report(
        report,
        [
            {"id": "ok1", "question": "q1", "golden_sql": "SELECT 1", "pred_sql": "SELECT 1", "correct": True},
            {
                "id": "bad1",
                "question": "q2",
                "golden_sql": "SELECT 2",
                "pred_sql": "SELECT 22",
                "correct": False,
                "error": "结果不等价",
            },
            {"id": "bad2", "question": "q3", "golden_sql": None, "pred_sql": "SELECT 3", "correct": False},
        ],
    )
    failed = load_failed_cases(report)
    assert [c["id"] for c in failed] == ["bad1"]


def test_build_candidate_keeps_error_in_meta(tmp_path):
    """候选 = question + golden_sql；pred_sql/error 只进 meta 不做示例。"""
    case = {
        "id": "bad1",
        "question": "各州 GMV",
        "tags": ["JOIN", "TopN"],
        "golden_sql": "SELECT 1",
        "pred_sql": "SELECT 2",
        "error": "结果不等价",
        "correct": False,
    }
    cand = build_candidate(case, tmp_path / "execution_latest.json", datasource_id=None)

    assert cand["question"] == "各州 GMV" and cand["sql"] == "SELECT 1"
    assert cand["verified"] is False and cand["source"] == "eval" and cand["datasource_id"] is None
    assert cand["meta"]["pred_sql"] == "SELECT 2" and cand["meta"]["error"] == "结果不等价"
    assert cand["meta"]["tags"] == ["JOIN", "TopN"] and cand["meta"]["case_id"] == "bad1"


def test_import_skips_verified_same_question(tmp_path):
    """跳过规则（should_skip，main 实际调用）：同作用域同问题已 verified → 不降级；否则导入。"""
    store = ExampleStore(tmp_path / "e.json", seed=False)
    store.add("各州 GMV", "SELECT verified_version")  # 已验证知识

    # 同题同作用域已验证 → 跳过
    assert should_skip(store, {"question": "各州 GMV", "datasource_id": None, "workspace_id": 0}) is True
    # 不同作用域（数据源 / workspace 维度）/ 新问题 / store 为 None → 不跳过
    assert should_skip(store, {"question": "各州 GMV", "datasource_id": 7, "workspace_id": 0}) is False
    assert should_skip(store, {"question": "各州 GMV", "datasource_id": None, "workspace_id": 2}) is False
    assert should_skip(store, {"question": "新问题", "datasource_id": None, "workspace_id": 0}) is False
    assert should_skip(None, {"question": "任意", "datasource_id": None, "workspace_id": 0}) is False

    # 未跳过的候选导入；二次导入覆盖（幂等，不堆积）
    cand = {
        "question": "新问题",
        "sql": "SELECT golden",
        "verified": False,
        "datasource_id": None,
        "source": "eval",
        "meta": {},
    }
    store.add(**cand)
    store.add(**cand)
    candidates = [r for r in store.list() if not r["verified"]]
    assert len(candidates) == 1 and candidates[0]["source"] == "eval"
    assert store.search("新问题") == []  # 候选不进 few-shot（verified_only）


def test_compare_reports_markdown(tmp_path):
    """对比输出含总体变化、标签分解与 case 翻转列表。"""
    base = tmp_path / "base.json"
    after = tmp_path / "after.json"
    _write_report(
        base,
        [
            {"id": "c1", "question": "q1", "tags": ["JOIN"], "correct": False},
            {"id": "c2", "question": "q2", "tags": ["单表"], "correct": True},
        ],
    )
    base_data = json.loads(base.read_text(encoding="utf-8"))
    base_data["summary"]["accuracy"] = 0.5
    base_data["by_tag"] = {
        "JOIN": {"total": 1, "correct": 0, "accuracy": 0.0},
        "单表": {"total": 1, "correct": 1, "accuracy": 1.0},
    }
    base.write_text(json.dumps(base_data, ensure_ascii=False), encoding="utf-8")

    _write_report(
        after,
        [
            {"id": "c1", "question": "q1", "tags": ["JOIN"], "correct": True},
            {"id": "c2", "question": "q2", "tags": ["单表"], "correct": True},
        ],
    )
    after_data = json.loads(after.read_text(encoding="utf-8"))
    after_data["summary"]["accuracy"] = 1.0
    after_data["by_tag"] = {
        "JOIN": {"total": 1, "correct": 1, "accuracy": 1.0},
        "单表": {"total": 1, "correct": 1, "accuracy": 1.0},
    }
    after.write_text(json.dumps(after_data, ensure_ascii=False), encoding="utf-8")

    md = compare(
        json.loads(base.read_text(encoding="utf-8")),
        json.loads(after.read_text(encoding="utf-8")),
        "base.json",
        "after.json",
    )
    assert "+50.00pp" in md  # 总体 50% → 100%
    assert "JOIN" in md and "新增通过 1 例" in md and "c1" in md
    assert "新增失败 0 例" in md


def test_compare_reports_flags_sample_set_mismatch(tmp_path):
    """样本集不一致：新增 case 不算翻转，输出警示与新增/移除清单（外部 CR 反例回归）。"""
    base = tmp_path / "base.json"
    after = tmp_path / "after.json"
    _write_report(base, [{"id": "c1", "question": "q1", "tags": [], "correct": False}])
    _write_report(
        after,
        [
            {"id": "c1", "question": "q1", "tags": [], "correct": False},  # 旧题结果未变
            {"id": "new1", "question": "新增题", "tags": [], "correct": True},  # 新增的正确题
        ],
    )

    md = compare(
        json.loads(base.read_text(encoding="utf-8")),
        json.loads(after.read_text(encoding="utf-8")),
        "base.json",
        "after.json",
    )
    assert "样本集不一致" in md  # 显式警示
    assert "新增 case：new1" in md  # 单独列出，不混入翻转
    assert "新增通过 0 例" in md  # 新增正确题不算翻转（c1 未变化）
