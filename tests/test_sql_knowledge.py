"""SQL 示例库 + 术语库 + 门控工具 + API 测试（roadmap P1-3/P1-4）。

覆盖：store 持久化与中文检索命中、术语同义词命中、合并工具输出含术语+示例、
TestClient 走一遍增删查 API、sql-generation SKILL.md 仍可解析且依赖展开含 sql_context_search。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.tools.sql_context_tool import create_sql_context_tool
from app.datasources.context import use_datasource
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
    assert "复购率" in out  # 术语命中
    assert "购买 2 次以上" in out or "下单次数≥2" in out  # 术语口径
    assert "customer_state" in out or "示例" in out  # 相似示例段


def test_sql_context_tool_empty(tmp_path):
    """两段都空时给出明确提示。"""
    ex = ExampleStore(tmp_path / "e.json", seed=False)
    tm = TermStore(tmp_path / "t.json", seed=False)
    tool = create_sql_context_tool(ex, tm)
    out = tool.invoke({"question": "xyzzy 无关问题"})
    assert "未命中" in out


def test_platform_datasource_scope_isolation(tmp_path):
    """平台数据源：只收自己的数据源级知识，不串演示库全局知识（此前是整体禁用）。"""
    ex = ExampleStore(tmp_path / "e.json")  # 种子=演示作用域
    tm = TermStore(tmp_path / "t.json")
    # 给数据源 12 配一条专属示例与术语
    ex.add("各州的客户数量分布", "SELECT state, COUNT(*) FROM usr GROUP BY state", verified=True, datasource_id=12)
    tm.add("活跃用户", ["活客"], "近 30 天有下单的用户", datasource_id=12)
    tool = create_sql_context_tool(ex, tm)

    with use_datasource(12, 34):
        out = tool.invoke({"question": "各州的活跃用户客户数量分布"})

    # 命中数据源级知识
    assert "FROM usr" in out
    assert "活跃用户" in out
    # 不串入演示库知识（演示库种子示例用 customers 表、术语是 GMV/复购率/客单价）
    assert "customer_state" not in out
    assert "复购率" not in out

    # 数据源 13 什么都没配 → 未命中提示（而非旧的"未配置"禁用文案）
    with use_datasource(13, 34):
        out13 = tool.invoke({"question": "各州的客户数量分布"})
    assert "未命中" in out13
    assert "customer_state" not in out13

    # 演示库路径不受影响：仍命中全局种子
    out_demo = tool.invoke({"question": "各州的客户数量分布"})
    assert "customer_state" in out_demo


def test_candidate_examples_not_injected_until_verified(tmp_path):
    """候选（verified=False）不进 few-shot；转正（覆盖为 True）后生效。"""
    ex = ExampleStore(tmp_path / "e.json", seed=False)
    ex.add("各州的客户数量", "SELECT WRONG FROM t", verified=False)
    assert ex.search("各州的客户数量") == []  # verified_only 默认 True
    assert ex.search("各州的客户数量", verified_only=False) != []  # 管理端可见

    ex.add("各州的客户数量", "SELECT state FROM t GROUP BY state", verified=True)  # 转正覆盖
    hits = ex.search("各州的客户数量")
    assert len(hits) == 1 and "state" in hits[0]["sql"]
    assert hits[0]["verified"] is True


def test_example_scope_dedup_by_datasource(tmp_path):
    """去重键含作用域（datasource_id / workspace_id）：平台示例不覆盖演示库同题示例。"""
    ex = ExampleStore(tmp_path / "e.json", seed=False)
    ex.add("各州的客户数量", "SELECT a FROM demo_t")
    ex.add("各州的客户数量", "SELECT b FROM ds_t", datasource_id=7)
    assert len(ex.list()) == 2

    # 演示作用域检索拿到自己的版本，数据源 7 检索拿到自己的版本
    assert "demo_t" in ex.search("各州的客户数量")[0]["sql"]
    assert "ds_t" in ex.search("各州的客户数量", datasource_id=7)[0]["sql"]

    # 同作用域同题覆盖（转正即此机制），id 复用不失效
    first_id = [r for r in ex.list() if r.get("datasource_id") == 7][0]["id"]
    ex.add("各州的客户数量", "SELECT c FROM ds_t2", datasource_id=7)
    assert len(ex.list()) == 2
    assert [r for r in ex.list() if r.get("datasource_id") == 7][0]["id"] == first_id


def test_example_dedup_includes_workspace(tmp_path):
    """鉴权开启后两个 workspace 的演示作用域（datasource NULL）同题各自独立，不互相覆盖。"""
    ex = ExampleStore(tmp_path / "e.json", seed=False)
    ex.add("各州的客户数量", "SELECT a FROM ws1_t", workspace_id=1)
    ex.add("各州的客户数量", "SELECT b FROM ws2_t", workspace_id=2)
    assert len(ex.list()) == 2

    # 各 workspace 检索只看到自己的示例（workspace=0 看不到它们）
    assert "ws1_t" in ex.search("各州的客户数量", workspace_id=1)[0]["sql"]
    assert "ws2_t" in ex.search("各州的客户数量", workspace_id=2)[0]["sql"]
    assert ex.search("各州的客户数量") == []

    # 同 workspace 同题覆盖，另一 workspace 不受影响
    ex.add("各州的客户数量", "SELECT a2 FROM ws1_t2", workspace_id=1)
    assert len(ex.list()) == 2
    assert "ws2_t" in ex.search("各州的客户数量", workspace_id=2)[0]["sql"]


def test_term_match_scoped_by_datasource(tmp_path):
    """术语按作用域命中：平台数据源看不到演示全局术语。"""
    tm = TermStore(tmp_path / "t.json")
    tm.add("GMV", ["成交额"], "演示口径")  # 演示作用域（种子已有 GMV，这里覆盖定义便于断言）
    tm.add("动销率", ["动销"], "数据源口径", datasource_id=5)

    assert any(t["term"] == "动销率" for t in tm.match("动销怎么样", datasource_id=5))
    assert tm.match("动销怎么样") == []  # 演示路径看不到数据源 5 的术语
    assert tm.match("成交额趋势", datasource_id=5) == []  # 数据源 5 看不到演示 GMV
    assert any(t["term"] == "GMV" for t in tm.match("成交额趋势"))  # 演示路径正常


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


def test_api_sql_example_datasource_ownership(client):
    """datasource_id 有值时校验归属：不存在的数据源 404，不带 datasource_id 正常入演示作用域。"""
    resp = client.post(
        "/api/sql-examples",
        json={"question": "平台问题", "sql": "SELECT 1", "verified": True, "datasource_id": 999},
    )
    assert resp.status_code == 404

    resp = client.post(
        "/api/sql-examples",
        json={"question": "演示问题", "sql": "SELECT 2", "verified": True, "source": "chat"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["datasource_id"] is None and body["source"] == "chat"


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
