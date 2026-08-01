"""Text-to-SQL 核心测试

覆盖：
1. M-Schema 生成：含表名/字段/类型/注释；无注释字段不编造注释
2. schema_search 门控工具：正常返回 M-Schema；库缺失返回中文提示
3. SKILL.md 升级后仍可被 SkillContent.parse 解析
4. 依赖展开后 schema_search 出现在 expanded.tools（激活后可解锁）
"""

import sqlite3
from pathlib import Path

import pytest

from app.agents.tools.schema_tool import create_schema_search_tool
from app.skills.models import SkillContent
from app.text2sql.comments_ecommerce import ECOMMERCE_COMMENTS
from app.text2sql.m_schema import build_table_m_schema, generate_m_schema, list_tables

BUILTIN_DIR = Path(__file__).parent.parent / "app" / "skills" / "buildin"


@pytest.fixture
def ecommerce_db(tmp_path) -> str:
    """自建一个含 orders / customers 两张演示表的临时 SQLite 库。

    orders 有一列 note 故意不在注释字典里，用于验证"无注释不编造"。
    """
    db_path = tmp_path / "ecommerce.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY,
            customer_id TEXT,
            order_status TEXT,
            order_purchase_timestamp TEXT,
            note TEXT
        );
        CREATE TABLE customers (
            customer_id TEXT PRIMARY KEY,
            customer_city TEXT,
            customer_state TEXT
        );
        INSERT INTO orders VALUES ('o1', 'c1', 'delivered', '2017-10-02 10:56:33', 'x');
        INSERT INTO customers VALUES ('c1', 'sao paulo', 'SP');
        """
    )
    conn.commit()
    conn.close()
    return str(db_path)


# ========== M-Schema 生成 ==========


def test_build_table_m_schema_basic():
    text = build_table_m_schema(
        table_name="orders",
        columns=[("order_id", "TEXT"), ("customer_id", "TEXT")],
        table_comment="订单表",
        field_comments={"order_id": "订单ID", "customer_id": "客户ID"},
    )
    assert text == "# Table: orders, 订单表\n[(order_id:TEXT, 订单ID), (customer_id:TEXT, 客户ID)]"


def test_build_table_m_schema_omits_missing_comments():
    """无注释的字段/表只输出 (字段:类型)，不带逗号注释"""
    text = build_table_m_schema(
        table_name="mystery",
        columns=[("a", "INTEGER"), ("b", "TEXT")],
        table_comment="",
        field_comments={"a": "甲字段"},  # b 无注释
    )
    assert text == "# Table: mystery\n[(a:INTEGER, 甲字段), (b:TEXT)]"


def test_generate_m_schema_full(ecommerce_db):
    m_schema = generate_m_schema(ecommerce_db, comments=ECOMMERCE_COMMENTS)

    # 表名 + 表注释
    assert "# Table: orders, 订单表" in m_schema
    assert "# Table: customers, 客户表" in m_schema
    # 字段名 + 类型 + 注释
    assert "(order_id:TEXT, 订单ID)" in m_schema
    assert "(order_status:TEXT, 订单状态（delivered/shipped/canceled 等）)" in m_schema
    assert "(customer_state:TEXT, 客户所在州)" in m_schema
    # 时间字段类型为 TEXT
    assert "(order_purchase_timestamp:TEXT, 下单时间)" in m_schema
    # 表按名排序：customers 在 orders 之前
    assert m_schema.index("# Table: customers") < m_schema.index("# Table: orders")


def test_generate_m_schema_no_fabricated_comment(ecommerce_db):
    """字典里没有的 note 字段，必须输出 (note:TEXT)，不能凭空生成注释"""
    m_schema = generate_m_schema(ecommerce_db, comments=ECOMMERCE_COMMENTS)
    assert "(note:TEXT)" in m_schema
    # 确保 note 后面没有紧跟逗号注释
    assert "(note:TEXT," not in m_schema


def test_generate_m_schema_empty_comments(ecommerce_db):
    """传空注释字典 → 所有字段都无注释"""
    m_schema = generate_m_schema(ecommerce_db, comments={})
    assert "# Table: orders\n" in m_schema
    assert "(order_id:TEXT)" in m_schema
    assert "订单ID" not in m_schema


def test_generate_m_schema_missing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        generate_m_schema(str(tmp_path / "nope.db"))


def test_list_tables(ecommerce_db):
    assert list_tables(ecommerce_db) == ["customers", "orders"]


# ========== schema_search 门控工具 ==========


def test_schema_search_tool_returns_m_schema(ecommerce_db):
    tool = create_schema_search_tool(ecommerce_db)
    result = tool.invoke({"question": "查询各州的订单数"})
    assert "# Table: orders, 订单表" in result
    assert "(customer_state:TEXT, 客户所在州)" in result


def test_schema_search_tool_missing_db(tmp_path):
    """库文件不存在 → 返回明确中文提示，不抛异常"""
    tool = create_schema_search_tool(str(tmp_path / "missing.db"))
    result = tool.invoke({"question": "随便问"})
    assert "数据库文件不存在" in result


# ========== 升级后的 SKILL.md 仍合法 + 依赖展开解锁 schema_search ==========


@pytest.mark.parametrize("slug", ["schema-retrieval", "sql-generation"])
def test_upgraded_skill_md_parses(slug):
    raw = (BUILTIN_DIR / slug / "SKILL.md").read_text(encoding="utf-8")
    content = SkillContent.parse(raw)  # frontmatter 非法会抛异常
    assert content.frontmatter.slug == slug
    assert content.body.strip()


async def test_schema_retrieval_declares_schema_search_tool(skill_service):
    """schema-retrieval 直接声明 schema_search"""
    skill = await skill_service.get_skill("schema-retrieval")
    assert "schema_search" in skill.get_tools()


async def test_expand_exposes_schema_search(skill_service):
    """展开 sql-generation（依赖 schema-retrieval）后，schema_search 进入 expanded.tools，
    且 tools_of 在激活 schema-retrieval 时能解锁它。
    """
    expanded = await skill_service.expand_dependencies(["sql-generation"])
    slugs = {s.slug for s in expanded.skills}
    assert {"sql-generation", "schema-retrieval"} <= slugs
    assert "schema_search" in expanded.tools
    # 激活 schema-retrieval 才解锁 schema_search
    assert "schema_search" in expanded.tools_of({"schema-retrieval"})
    assert "schema_search" not in expanded.tools_of({"sql-generation"})
