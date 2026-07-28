"""Text-to-SQL 执行准确率评估的离线测试（roadmap P1-2）。

**不调用 LLM**：LLM 输出不确定、依赖网络与密钥，进不了 CI。这里只测三样可确定的东西：
1. dataset.json 里每条 golden_sql 都能在合成演示库上用**原生 sqlite3** 执行成功且有结果
   —— golden 是评估的"标准答案基准"，它自己必须先站得住。
   （刻意绕开 sql_guard.validate_sql：main 上有 SELECT 别名误报 bug，PR #5 才修，
    用原生 sqlite3 执行可不受其影响，见任务约定。）
2. common.py 的结果集归一化对比：列序无关 / 行序 / 浮点容差 / 真实差异判 False。
3. build_report 对 mock 逐例结果能聚合出正确的总分与按标签分桶。

按项目约定不改 tests/conftest.py，需要的 fixture 都写在本文件里。
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# scripts/ 不是包，手动加入 import 路径（与 test_demo_data.py 一致）
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import import_ecommerce as ie  # noqa: E402

from evals.text2sql.common import (  # noqa: E402
    build_report,
    compare_result_sets,
    golden_has_order_by,
)

DATASET_PATH = Path(__file__).parent.parent / "evals" / "text2sql" / "dataset.json"
DATASET = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
EXPECTED_TAGS = {"单表聚合", "多表JOIN", "时间过滤", "TopN", "CTE"}


@pytest.fixture(scope="module")
def synthetic_db(tmp_path_factory) -> str:
    """整模块共用一套合成演示库（seed=42，可复现）。"""
    db_path = tmp_path_factory.mktemp("t2s") / "ecommerce.db"
    ie.build_demo_db(db_path=db_path, synthetic=True, seed=42)
    return str(db_path)


def _run(db_path: str, sql: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ========== 数据集自检：golden_sql 都能跑、都有结果 ==========


def test_dataset_size_and_tags():
    """规模 25~30，标签只用约定的 5 个分层。"""
    assert 25 <= len(DATASET) <= 30, f"用例数 {len(DATASET)} 不在 25~30 区间"
    ids = [c["id"] for c in DATASET]
    assert len(ids) == len(set(ids)), "存在重复 id"
    for c in DATASET:
        assert c["question"].strip()
        assert c["golden_sql"].strip()
        assert c["tags"], f"{c['id']} 缺少 tags"
        assert set(c["tags"]) <= EXPECTED_TAGS, f"{c['id']} 含未知标签 {c['tags']}"


def test_all_five_layers_covered():
    """五个难度分层都要有覆盖。"""
    used = set()
    for c in DATASET:
        used.update(c["tags"])
    assert used >= EXPECTED_TAGS, f"缺少分层: {EXPECTED_TAGS - used}"


@pytest.mark.parametrize("case", DATASET, ids=[c["id"] for c in DATASET])
def test_golden_sql_executes_with_results(case, synthetic_db):
    """每条 golden_sql 原生执行成功，且至少返回一行（标准答案不能为空）。"""
    rows = _run(synthetic_db, case["golden_sql"])
    assert rows, f"{case['id']} 的 golden_sql 返回空结果集"


# ========== golden_has_order_by 判定 ==========


def test_golden_has_order_by_detection():
    assert golden_has_order_by("SELECT a FROM t ORDER BY a") is True
    assert golden_has_order_by("SELECT a, COUNT(*) FROM t GROUP BY a") is False
    # 只看最外层：CTE 内部的 ORDER BY 不算
    assert golden_has_order_by("WITH x AS (SELECT a FROM t ORDER BY a) SELECT a FROM x") is False
    # 解析失败保守当作有序
    assert golden_has_order_by("这不是SQL ###") is True


# ========== 结果集对比：正例 / 反例 ==========


def test_compare_identical_sets():
    a = [("SP", 100.0), ("RJ", 50.0)]
    assert compare_result_sets(a, a, order_sensitive=False) is True


def test_compare_column_order_invariant():
    """列序打乱应判等（列序无关）。"""
    golden = [("SP", 100.0), ("RJ", 50.0)]
    pred = [(100.0, "SP"), (50.0, "RJ")]
    assert compare_result_sets(golden, pred, order_sensitive=False) is True


def test_compare_row_order_insensitive_when_unordered():
    """golden 无 ORDER BY → 行序无关，打乱行仍判等。"""
    golden = [("SP", 1), ("RJ", 2), ("MG", 3)]
    pred = [("MG", 3), ("SP", 1), ("RJ", 2)]
    assert compare_result_sets(golden, pred, order_sensitive=False) is True


def test_compare_row_order_sensitive_when_ordered():
    """golden 有 ORDER BY → 行序敏感，打乱行必须判不等。"""
    golden = [("SP", 1), ("RJ", 2)]
    pred = [("RJ", 2), ("SP", 1)]
    assert compare_result_sets(golden, pred, order_sensitive=True) is False


def test_compare_float_tolerance():
    """未 ROUND 的浮点与 ROUND 到 2 位应判等（量化吸收末位差）。"""
    golden = [(12345.68,)]
    pred = [(12345.6789,)]
    assert compare_result_sets(golden, pred, order_sensitive=False) is True


def test_compare_int_float_equivalence():
    """COUNT 的 int 与等值 float 应判等。"""
    assert compare_result_sets([(5,)], [(5.0,)], order_sensitive=False) is True


def test_compare_real_difference_is_false():
    """真实数值差异必须判 False（容差之外）。"""
    assert compare_result_sets([(100.0,)], [(101.0,)], order_sensitive=False) is False
    # 行数不同也应判 False
    assert compare_result_sets([("SP", 1), ("RJ", 2)], [("SP", 1)], order_sensitive=False) is False


def test_compare_multiset_counts_matter():
    """行序无关不等于去重：重复行的重数不同应判 False。"""
    assert compare_result_sets([("SP",), ("SP",)], [("SP",)], order_sensitive=False) is False


def test_normalize_none_handling():
    """含 NULL 的行不应抛异常，且能正确判等。"""
    assert compare_result_sets([(None, 1)], [(1, None)], order_sensitive=False) is True


# ========== build_report 聚合 ==========


def test_build_report_aggregates_overall_and_by_tag():
    mock = [
        {"id": "a", "tags": ["单表聚合"], "correct": True},
        {"id": "b", "tags": ["单表聚合", "TopN"], "correct": False},
        {"id": "c", "tags": ["多表JOIN"], "correct": True},
        {"id": "d", "tags": ["TopN"], "correct": True},
    ]
    report = build_report(mock)

    # 总分：4 例 3 对
    assert report["summary"] == {"total": 4, "correct": 3, "accuracy": 0.75}

    # 按标签分桶（一个用例的多个标签各计一次）
    # 单表聚合: a(对) + b(错) = 1/2；TopN: b(错) + d(对) = 1/2；多表JOIN: c(对) = 1/1
    assert report["by_tag"]["单表聚合"] == {"total": 2, "correct": 1, "accuracy": 0.5}
    assert report["by_tag"]["TopN"] == {"total": 2, "correct": 1, "accuracy": 0.5}
    assert report["by_tag"]["多表JOIN"] == {"total": 1, "correct": 1, "accuracy": 1.0}

    # 明细原样带出
    assert len(report["cases"]) == 4


def test_build_report_empty():
    """空输入不崩，总分为 0。"""
    report = build_report([])
    assert report["summary"] == {"total": 0, "correct": 0, "accuracy": 0.0}
    assert report["by_tag"] == {}
