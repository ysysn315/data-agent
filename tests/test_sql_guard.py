"""SQL 校验层测试：validate_sql（sql_guard）+ execute_sql 集成。

自建临时库 fixture（sql_guard_db），不动 conftest。
"""
import json
import sqlite3

import pytest

from app.agents.tools.sql_guard import validate_sql
from app.agents.tools.sql_tool import create_execute_sql_tool

# 供 validate_sql 直接校验用的 schema
SCHEMA = {
    "orders": ["order_id", "customer_state", "price"],
    "customers": ["customer_id", "name"],
}


@pytest.fixture
def sql_guard_db(tmp_path) -> str:
    """本测试专用的最小 SQLite 库（不复用 conftest 的 demo_db）"""
    db_path = tmp_path / "guard.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            customer_state TEXT,
            price REAL
        );
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            name TEXT
        );
        INSERT INTO orders VALUES (1, 'SP', 100.0), (2, 'RJ', 50.5), (3, 'SP', 30.0);
        INSERT INTO customers VALUES (1, '张三'), (2, '李四');
        """
    )
    conn.commit()
    conn.close()
    return str(db_path)


# ---------- validate_sql：自动 LIMIT ----------

def test_select_auto_limit_added():
    r = validate_sql("SELECT price FROM orders", schema=SCHEMA, default_limit=1000)
    assert r.ok
    assert "LIMIT 1000" in r.fixed_sql.upper()


def test_existing_limit_not_duplicated():
    r = validate_sql("SELECT price FROM orders LIMIT 5", schema=SCHEMA)
    assert r.ok
    assert r.fixed_sql.upper().count("LIMIT") == 1
    assert "LIMIT 5" in r.fixed_sql.upper()


# ---------- validate_sql：CTE 名不被当未知表（关键回归，SQLBot 7118b40 的坑）----------

def test_cte_name_not_treated_as_unknown_table():
    sql = "WITH recent AS (SELECT order_id, price FROM orders) SELECT price FROM recent"
    r = validate_sql(sql, schema=SCHEMA)
    assert r.ok, f"CTE 名被误当未知表: {r.error}"
    assert "LIMIT" in r.fixed_sql.upper()


def test_cte_referencing_unknown_real_table_still_caught():
    # CTE 内部引用了真实不存在的表，仍要报错（证明排除的是 CTE 名而非放弃校验）
    sql = "WITH t AS (SELECT * FROM ghost_table) SELECT * FROM t"
    r = validate_sql(sql, schema=SCHEMA)
    assert not r.ok
    assert "ghost_table" in r.error


# ---------- validate_sql：只读 / 单语句 / 语法 ----------

def test_multi_statement_rejected():
    r = validate_sql("SELECT 1; DROP TABLE orders", schema=SCHEMA)
    assert not r.ok
    assert "多语句" in r.error or "单条" in r.error


def test_update_rejected():
    r = validate_sql("UPDATE orders SET price = 0", schema=SCHEMA)
    assert not r.ok
    assert "UPDATE" in r.error


def test_drop_rejected():
    r = validate_sql("DROP TABLE orders", schema=SCHEMA)
    assert not r.ok
    assert "DROP" in r.error


def test_with_insert_rejected_at_ast():
    # 首词是 WITH，但 AST 最外层是 Insert —— 字符串首词判断会漏，AST 判断能拦
    r = validate_sql("WITH x AS (SELECT 1) INSERT INTO orders SELECT 4, 'MG', 1 FROM x", schema=SCHEMA)
    assert not r.ok
    assert "INSERT" in r.error


def test_syntax_error_reports_position():
    r = validate_sql("SELECT FROM WHERE", schema=SCHEMA)
    assert not r.ok
    assert "语法错误" in r.error
    # sqlglot 的报错带行列位置
    assert "Line" in r.error or "Col" in r.error


# ---------- validate_sql：未知表 / 未知列，文案含候选 ----------

def test_unknown_table_lists_candidates():
    r = validate_sql("SELECT * FROM nope", schema=SCHEMA)
    assert not r.ok
    assert "nope" in r.error
    assert "orders" in r.error and "customers" in r.error  # 列出可用表


def test_unknown_column_lists_candidates():
    r = validate_sql("SELECT nonexistent FROM orders", schema=SCHEMA)
    assert not r.ok
    assert "nonexistent" in r.error
    # 文案含该表真实列，供模型自纠
    assert "price" in r.error and "customer_state" in r.error


def test_qualified_unknown_column_caught():
    r = validate_sql("SELECT o.bad_col FROM orders o", schema=SCHEMA)
    assert not r.ok
    assert "bad_col" in r.error


def test_known_column_and_join_pass():
    # 多表 JOIN：限定列都存在应通过（且不因无法定位非限定列而误报）
    sql = (
        "SELECT o.price, c.name FROM orders o "
        "JOIN customers c ON o.order_id = c.customer_id"
    )
    r = validate_sql(sql, schema=SCHEMA)
    assert r.ok, r.error


def test_no_schema_skips_table_column_check():
    # 不给 schema 时只做语法 / 只读 / LIMIT，不校验表列
    r = validate_sql("SELECT whatever FROM some_unknown_table")
    assert r.ok
    assert "LIMIT" in r.fixed_sql.upper()


# ---------- execute_sql 集成 ----------

def test_execute_sql_good_query_returns_data(sql_guard_db):
    execute_sql = create_execute_sql_tool(sql_guard_db)
    out = execute_sql.invoke({"sql": "SELECT SUM(price) FROM orders"})
    data = json.loads(out)
    assert data["rows"][0][0] == 180.5


def test_execute_sql_auto_limit_applied(sql_guard_db):
    execute_sql = create_execute_sql_tool(sql_guard_db)
    # 无 LIMIT 的宽查询，自动补 LIMIT 后仍能正常出数
    out = execute_sql.invoke({"sql": "SELECT order_id, price FROM orders"})
    data = json.loads(out)
    assert data["row_count"] == 3


def test_execute_sql_bad_column_returns_guard_error(sql_guard_db):
    execute_sql = create_execute_sql_tool(sql_guard_db)
    out = execute_sql.invoke({"sql": "SELECT no_such_col FROM orders"})
    assert out.startswith("SQL 校验失败:")
    assert "no_such_col" in out


def test_execute_sql_unknown_table_returns_guard_error(sql_guard_db):
    execute_sql = create_execute_sql_tool(sql_guard_db)
    out = execute_sql.invoke({"sql": "SELECT * FROM ghost"})
    assert out.startswith("SQL 校验失败:")
    assert "ghost" in out


def test_execute_sql_cte_query_works(sql_guard_db):
    execute_sql = create_execute_sql_tool(sql_guard_db)
    out = execute_sql.invoke(
        {"sql": "WITH sp AS (SELECT price FROM orders WHERE customer_state='SP') SELECT SUM(price) FROM sp"}
    )
    data = json.loads(out)
    assert data["rows"][0][0] == 130.0


def test_execute_sql_can_query_sqlite_master(sql_guard_db):
    # 文档承诺可以查 sqlite_master 拿表结构，不能被当未知表拦下
    execute_sql = create_execute_sql_tool(sql_guard_db)
    out = execute_sql.invoke({"sql": "SELECT name FROM sqlite_master WHERE type='table'"})
    data = json.loads(out)
    names = {row[0] for row in data["rows"]}
    assert "orders" in names and "customers" in names
