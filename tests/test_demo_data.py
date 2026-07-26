"""演示数据导入（合成模式）测试 —— 对应 roadmap P0-1。

覆盖：
1. 各表行数 > 0；
2. 外键一致性（order_items / payments / orders 的关联列都能对上）；
3. 时间字段可被 SQLite date() 解析，且落在 2016~2018；
4. execute_sql 工具（app/agents/tools/sql_tool.py）能对生成的库查询成功。

注意：按项目约定不改 tests/conftest.py，需要的 fixture 写在本文件里。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

# scripts/ 不是包，手动加入 import 路径
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import import_ecommerce as ie  # noqa: E402

from app.agents.tools.sql_tool import create_execute_sql_tool  # noqa: E402


@pytest.fixture
def synthetic_db(tmp_path) -> str:
    """在临时目录生成一套合成演示库，返回其路径。"""
    db_path = tmp_path / "ecommerce.db"
    counts = ie.build_demo_db(db_path=db_path, synthetic=True, seed=42)
    # 附带把行数塞进对象，方便断言（用属性挂在 str 上不行，改用返回 tuple 太重，
    # 这里直接返回路径，行数在用例里现查）
    assert counts  # 建库确实返回了行数字典
    return str(db_path)


def test_all_tables_non_empty(synthetic_db):
    """六张表都应有数据。"""
    conn = sqlite3.connect(synthetic_db)
    try:
        for table in ie.TABLES:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n > 0, f"{table} 表为空"
    finally:
        conn.close()


def test_indexes_created(synthetic_db):
    """常用索引应已建立。"""
    conn = sqlite3.connect(synthetic_db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchone()[0]
        assert n >= 10
    finally:
        conn.close()


def test_foreign_key_consistency(synthetic_db):
    """所有跨表关联列都必须落在父表主键集合内（无孤儿行）。"""
    conn = sqlite3.connect(synthetic_db)
    checks = {
        "order_items.order_id → orders":
            "SELECT COUNT(*) FROM order_items "
            "WHERE order_id NOT IN (SELECT order_id FROM orders)",
        "order_items.product_id → products":
            "SELECT COUNT(*) FROM order_items "
            "WHERE product_id NOT IN (SELECT product_id FROM products)",
        "order_items.seller_id → sellers":
            "SELECT COUNT(*) FROM order_items "
            "WHERE seller_id NOT IN (SELECT seller_id FROM sellers)",
        "payments.order_id → orders":
            "SELECT COUNT(*) FROM payments "
            "WHERE order_id NOT IN (SELECT order_id FROM orders)",
        "orders.customer_id → customers":
            "SELECT COUNT(*) FROM orders "
            "WHERE customer_id NOT IN (SELECT customer_id FROM customers)",
    }
    try:
        for label, sql in checks.items():
            orphans = conn.execute(sql).fetchone()[0]
            assert orphans == 0, f"{label} 存在 {orphans} 条孤儿行"
    finally:
        conn.close()


def test_timestamps_parseable_and_in_range(synthetic_db):
    """下单时间应能被 SQLite date() 解析，且年份落在 2016~2018。"""
    conn = sqlite3.connect(synthetic_db)
    try:
        unparseable = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE date(order_purchase_timestamp) IS NULL"
        ).fetchone()[0]
        assert unparseable == 0

        lo, hi = conn.execute(
            "SELECT MIN(date(order_purchase_timestamp)), "
            "MAX(date(order_purchase_timestamp)) FROM orders"
        ).fetchone()
        assert lo >= "2016-01-01"
        assert hi <= "2018-12-31"
    finally:
        conn.close()


def test_reproducible_with_seed(tmp_path):
    """同一种子两次生成，行数应完全一致（可复现）。"""
    a = ie.build_demo_db(tmp_path / "a.db", synthetic=True, seed=7)
    b = ie.build_demo_db(tmp_path / "b.db", synthetic=True, seed=7)
    assert a == b


def test_execute_sql_tool_works(synthetic_db):
    """execute_sql 工具能对生成的库跑通聚合查询并返回结构化 JSON。"""
    execute_sql = create_execute_sql_tool(synthetic_db)
    # 各州客户数 Top5 —— 典型 demo 问题
    result = execute_sql.invoke(
        {
            "sql": "SELECT customer_state, COUNT(*) AS n "
                   "FROM customers GROUP BY customer_state "
                   "ORDER BY n DESC",
            "limit": 5,
        }
    )
    payload = json.loads(result)
    assert payload["columns"] == ["customer_state", "n"]
    assert payload["row_count"] == 5
    # SP 是最大州，应排在首位
    assert payload["rows"][0][0] == "SP"


def test_execute_sql_join_across_tables(synthetic_db):
    """跨表 JOIN 也应正常：验证订单项能关联到订单。"""
    execute_sql = create_execute_sql_tool(synthetic_db)
    result = execute_sql.invoke(
        {
            "sql": "SELECT o.order_status, ROUND(SUM(i.price), 2) AS gmv "
                   "FROM orders o JOIN order_items i ON o.order_id = i.order_id "
                   "GROUP BY o.order_status ORDER BY gmv DESC",
            "limit": 10,
        }
    )
    payload = json.loads(result)
    assert payload["row_count"] > 0
    assert payload["columns"] == ["order_status", "gmv"]
