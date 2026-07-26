"""SQL 示例库 + 术语库 + 门控工具 + API 测试（roadmap P1-3/P1-4）。

覆盖：store 持久化与中文检索命中、术语同义词命中、合并工具输出含术语+示例、
TestClient 走一遍增删查 API、sql-generation SKILL.md 仍可解析且依赖展开含 sql_context_search。
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.tools.sql_context_tool import create_sql_context_tool
from app.text2sql.examples import SEED_EXAMPLES, ExampleStore
from app.text2sql.terminology import TermStore

BUILTIN_DIR = Path(__file__).parent.parent / "app" / "skills" / "buildin"


# ========== ExampleStore ==========

def test_example_store_seed_and_persistence(tmp_path):
    """种子灌入 + 持久化：新实例能读回，且不重复灌种。"""
    path = tmp_path / "sql_examples.json"
    store = ExampleStore(path)
    assert len(store.list()) == len(SEED_EXAMPLES)
    assert path.exists()

    # 同路径新实例：从文件加载，数量不变（不重复灌种）
    store2 = ExampleStore(path)
    assert len(store2.list()) == len(SEED_EXAMPLES)


def test_example_store_search_chinese_hit(tmp_path):
    """中文问题按 jieba 词元重叠检索到相关示例。"""
    store = ExampleStore(tmp_path / "e.json")
    hits = store.search("帮我看看各个州的客户数量", top_k=3)
    assert hits, "应命中'各州的客户数量分布'示例"
    assert "customer_state" in hits[0]["sql"]

    # 不相关问题不命中
    assert store.search("今天天气怎么样") == []


def test_example_store_add_update_delete(tmp_path):
    """反馈入库：新增、同问题更新、删除。"""
    store = ExampleStore(tmp_path / "e.json", seed=False)
    rec = store.add("每个卖家的销售额", "SELECT seller_id FROM order_items", verified=True)
    assert rec["verified"] is True
    assert len(store.list()) == 1

    # 同 question 覆盖而非新增
    store.add("每个卖家的销售额", "SELECT seller_id, SUM(price) FROM order_items GROUP BY seller_id")
    assert len(store.list()) == 1
    assert "SUM(price)" in store.list()[0]["sql"]

    assert store.delete(rec["id"]) is True
    assert store.list() == []
    assert store.delete("nope") is False


# ========== TermStore ==========

def test_term_store_seed_and_synonym_match(tmp_path):
    """种子术语 + 同义词子串命中。"""
    store = TermStore(tmp_path / "t.json")
    terms = {t["term"] for t in store.list()}
    assert {"GMV", "复购率", "客单价"} <= terms

    # 用同义词"成交额"命中 GMV
    hits = store.match("2018 年的成交额是多少")
    assert any(h["term"] == "GMV" for h in hits)

    # 术语原词命中（大小写不敏感）
    assert any(h["term"] == "GMV" for h in store.match("统计 gmv 趋势"))

    # 未命中
    assert store.match("查一下订单状态") == []


def test_term_store_crud_persistence(tmp_path):
    path = tmp_path / "t.json"
    store = TermStore(path, seed=False)
    store.add("动销率", ["动销"], "有销量的商品占比", sql_hint="有订单商品数 / 总商品数")
    assert len(store.list()) == 1

    store2 = TermStore(path)  # 已有文件，不灌种
    assert len(store2.list()) == 1
    assert store2.match("动销怎么样")[0]["term"] == "动销率"

    assert store2.delete("动销率") is True
    assert store2.delete("动销率") is False


# ========== 合并门控工具 ==========

def test_sql_context_tool_contains_term_and_example(tmp_path):
    """一个工具同时返回术语解释 + 相似示例。"""
    ex = ExampleStore(tmp_path / "e.json")
    tm = TermStore(tmp_path / "t.json")
    tool = create_sql_context_tool(ex, tm)

    out = tool.invoke({"question": "各州的复购率是多少"})
    assert "复购率" in out                     # 术语命中
    assert "购买 2 次以上" in out or "下单次数≥2" in out  # 术语口径
    assert "customer_state" in out or "示例" in out       # 相似示例段


def test_sql_context_tool_empty(tmp_path):
    """两段都空时给出明确提示。"""
    ex = ExampleStore(tmp_path / "e.json", seed=False)
    tm = TermStore(tmp_path / "t.json", seed=False)
    tool = create_sql_context_tool(ex, tm)
    out = tool.invoke({"question": "xyzzy 无关问题"})
    assert "未命中" in out


# ========== SKILL.md 依赖展开 ==========

async def test_sql_generation_skill_declares_context_tool(skill_service):
    """sql-generation 仍可解析，且依赖展开的 tools 含 sql_context_search。"""
    skill = await skill_service.get_skill("sql-generation")
    assert skill is not None
    assert "sql_context_search" in skill.get_tools()

    expanded = await skill_service.expand_dependencies(["sql-generation"])
    assert "sql_context_search" in expanded.tools
    # 激活 sql-generation 后，该工具应在其直接声明的工具集中
    assert "sql_context_search" in expanded.tools_of({"sql-generation"})


# ========== API（TestClient 增删查）==========

@pytest.fixture
def client(tmp_path):
    from app.core.dependencies import get_example_store, get_term_store
    from app.main import app

    ex = ExampleStore(tmp_path / "sql_examples.json")
    tm = TermStore(tmp_path / "terminology.json")
    app.dependency_overrides[get_example_store] = lambda: ex
    app.dependency_overrides[get_term_store] = lambda: tm
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_sql_examples_crud(client):
    # 列表（含种子）
    resp = client.get("/api/sql-examples")
    assert resp.status_code == 200
    assert len(resp.json()) == len(SEED_EXAMPLES)

    # 新增（反馈入库）
    resp = client.post(
        "/api/sql-examples",
        json={"question": "每个城市的订单数", "sql": "SELECT customer_city FROM customers", "verified": True},
    )
    assert resp.status_code == 201
    new_id = resp.json()["id"]

    assert len(client.get("/api/sql-examples").json()) == len(SEED_EXAMPLES) + 1

    # 删除
    assert client.delete(f"/api/sql-examples/{new_id}").status_code == 204
    assert client.delete(f"/api/sql-examples/{new_id}").status_code == 404


def test_api_terminology_crud(client):
    resp = client.get("/api/terminology")
    assert resp.status_code == 200
    assert any(t["term"] == "GMV" for t in resp.json())

    resp = client.post(
        "/api/terminology",
        json={"term": "转化率", "synonyms": ["下单转化"], "definition": "下单用户 / 访问用户"},
    )
    assert resp.status_code == 201
    assert resp.json()["term"] == "转化率"

    assert client.delete("/api/terminology/转化率").status_code == 204
    assert client.delete("/api/terminology/转化率").status_code == 404
