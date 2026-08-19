"""评测回流工具测试：export_failures 的候选构建/跳过规则 + compare_reports 的对比输出。

export_failures 的 main 走 settings+DB 装配（集成动作），这里测纯函数层
（load_failed_cases / build_candidate）与通过 ExampleStore（JSON 版）验证跳过规则；
compare_reports 全纯函数直测。
"""

from __future__ import annotations

import json

import pytest

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
        "difficulty": "hard",
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
    assert cand["meta"]["difficulty"] == "hard"


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
    base_data["by_difficulty"] = {
        "easy": {"total": 1, "correct": 1, "accuracy": 1.0},
        "hard": {"total": 1, "correct": 0, "accuracy": 0.0},
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
    after_data["by_difficulty"] = {
        "easy": {"total": 1, "correct": 1, "accuracy": 1.0},
        "hard": {"total": 1, "correct": 1, "accuracy": 1.0},
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
    assert "按难度" in md and "| 困难 | 0.00%（0/1） | 100.00%（1/1） | +100.00pp |" in md


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


def test_compare_reports_flags_dataset_content_change(tmp_path):
    """ID 相同但题面/golden 等内容发生变化时，也不能当作严格可比。"""
    base = tmp_path / "base.json"
    after = tmp_path / "after.json"
    cases = [{"id": "c1", "question": "q1", "tags": [], "correct": True}]
    _write_report(base, cases)
    _write_report(after, cases)

    base_data = json.loads(base.read_text(encoding="utf-8"))
    after_data = json.loads(after.read_text(encoding="utf-8"))
    base_data["meta"] = {"dataset_fingerprint": "old"}
    after_data["meta"] = {"dataset_fingerprint": "new"}

    md = compare(base_data, after_data, "base.json", "after.json")
    assert "数据集内容指纹不同" in md


def test_reranker_lazy_import_without_torch(monkeypatch):
    """reranker 惰性导入回归：未装 torch 时 import 模块不炸，构造才报错（评审修复）。

    修复前 torch/FlagEmbedding 是顶层 import——未安装时整个 evals.rag 不可 import。
    """
    import sys

    import app.rag.reranker as mod

    # 模拟 torch 缺失：从 sys.modules 移除并让 import 抛 ImportError
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setitem(sys.modules, "FlagEmbedding", None)
    with pytest.raises(RuntimeError, match="torch/FlagEmbedding"):
        mod.BGEReranker("x")  # 构造时才报错，且是 RuntimeError 带回退指引


def test_rerank_prefer_bge_raises_without_bge(monkeypatch):
    """rerank_prefer 显式控制回归：prefer=bge 失败必须抛错而非静默回退 LLM（防消融混组）。"""
    import sys

    import evals.rag.common as common

    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setitem(sys.modules, "FlagEmbedding", None)
    with pytest.raises(RuntimeError, match="torch"):
        common._build_eval_rerankers(None, prefer="bge")  # 强制 bge 不可用时抛错

    # prefer=llm 则不碰 BGE；LLM 工厂 mock 掉（CI 无 LLM_API_KEY，不能让单测依赖真实配置）
    class _FakeLLM:
        pass

    monkeypatch.setattr(common.LLMFactory, "create_llm", staticmethod(lambda **kw: _FakeLLM()))
    reranker, llm = common._build_eval_rerankers(None, prefer="llm")
    assert reranker is None and isinstance(llm, _FakeLLM)


def test_execution_eval_rate_limit_helpers():
    """限流退避判定回归：_is_rate_limited 按 error 文本识别 429。"""
    from evals.text2sql.run_execution_eval import (
        RATE_LIMIT_BASE_WAIT,
        RATE_LIMIT_MAX_ATTEMPTS,
        _is_rate_limited,
        _rate_limit_wait,
    )

    assert _is_rate_limited({"error": "执行异常: Error code: 429 - 限流"}) is True
    assert _is_rate_limited({"error": "校验失败: 表不存在"}) is False
    assert _is_rate_limited({"error": None}) is False
    assert RATE_LIMIT_MAX_ATTEMPTS >= 3 and RATE_LIMIT_BASE_WAIT >= 1  # 常量在合理范围
    # 指数退避序列：5→10→20→40 封顶 30（修复前是线性 5/10/15/20——评审指出名实不符）
    assert [_rate_limit_wait(a) for a in range(5)] == [5, 10, 20, 30, 30]
