"""知识图谱测试（E 轮，全离线：LLM 抽取用假模型，不调真实 API）。

覆盖：
- extractor：合法 JSON / ```json 栅栏+前后杂文本 / 非法输出容错 / 空文本不调 LLM
- store：(s,p,o) 幂等、重启持久性（同 tmp sqlite 重开 engine）、NetworkX 镜像写后失效重建
- 种子：首启表空灌种、二次启动幂等、与术语库口径对齐
- service：query_entity 深度与方向、find_path 谓词链（含反向跳）、extract_and_add
- graph_search 门控工具：文本化子图 / 未命中相近实体提示
- API：TestClient + dependency_overrides 走全部 5 个端点
- SKILL.md：可解析且依赖展开含 graph_search
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.agents.tools.graph_tool import create_graph_search_tool
from app.db.engine import create_engine_and_sessionmaker, init_db
from app.db.repositories import GraphTripleRepository
from app.graph.extractor import Triple, extract_triples
from app.graph.service import GraphService
from app.graph.store import SEED_TRIPLES, GraphStore


class FakeLLM:
    """按脚本吐文本的假模型（只实现 extractor 依赖的 .invoke 协议）。"""

    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls = 0

    def invoke(self, prompt: str) -> AIMessage:
        self.calls += 1
        return AIMessage(content=self.responses.pop(0) if self.responses else "[]")


@pytest.fixture
def graph_env(tmp_path):
    """tmp sqlite + 独立事件循环 runner（模拟 app.db.run_sync 的 sync→async 桥）。

    make_repo 每次调用新开一个 engine 指向同一库文件 —— 调两次即模拟「重启」。
    """
    loop = asyncio.new_event_loop()
    engines = []
    url = f"sqlite+aiosqlite:///{tmp_path / 'graph.db'}"

    def run(coro):
        return loop.run_until_complete(coro)

    def make_repo() -> GraphTripleRepository:
        engine, sm = create_engine_and_sessionmaker(url)
        engines.append(engine)
        run(init_db(engine))
        return GraphTripleRepository(sm)

    try:
        yield make_repo, run
    finally:
        for engine in engines:
            run(engine.dispose())
        loop.close()


# ========== extractor：假 LLM 三态 ==========

def test_extract_triples_valid_json():
    llm = FakeLLM(
        '[{"subject": "订单", "predicate": "属于", "object": "客户"},'
        ' {"subject": "订单", "predicate": "属于", "object": "客户"}]'
    )
    triples = extract_triples("订单属于客户", llm)
    # 批内重复只留一条；来源默认 llm
    assert [t.to_dict() for t in triples] == [
        {"subject": "订单", "predicate": "属于", "object": "客户", "source": "llm"}
    ]


def test_extract_triples_with_fence_and_noise():
    """```json 栅栏包裹 + 前后杂文本（含干扰性中括号）仍可解析。"""
    raw = (
        "好的，抽取[结果]如下：\n```json\n"
        '[{"subject": "GMV", "predicate": "计算自", "object": "订单项价格"}]'
        "\n```\n以上就是全部三元组。"
    )
    triples = extract_triples("GMV 计算自订单项价格", FakeLLM(raw))
    assert len(triples) == 1
    assert (triples[0].subject, triples[0].predicate, triples[0].object) == ("GMV", "计算自", "订单项价格")


def test_extract_triples_invalid_output(loguru_capture):
    # 完全不是 JSON / 顶层不是数组且内部无数组 → []（告警不抛错）
    assert extract_triples("文本", FakeLLM("我不会输出 JSON")) == []
    assert extract_triples("文本", FakeLLM('{"foo": 1}')) == []
    assert any("无法解析" in r for r in loguru_capture)
    # 坏元素（缺字段/空值/非对象）跳过，好元素保留
    triples = extract_triples("文本", FakeLLM(
        '[{"subject": "A", "predicate": "", "object": "B"}, "字符串元素",'
        ' {"subject": "A", "predicate": "p", "object": "B"}]'
    ))
    assert [t.to_dict()["predicate"] for t in triples] == ["p"]


def test_extract_triples_empty_text_skips_llm():
    llm = FakeLLM()
    assert extract_triples("", llm) == []
    assert extract_triples("   \n", llm) == []
    assert llm.calls == 0  # 空文本不浪费 LLM 调用


# ========== store：幂等 / 重启持久性 / 镜像失效重建 ==========

def test_store_idempotent_add(graph_env):
    make_repo, run = graph_env
    store = GraphStore(make_repo(), runner=run, seed=False)
    t = {"subject": "订单", "predicate": "属于", "object": "客户", "source": "manual"}

    assert store.add_triples([t, t]) == 1      # 批内重复
    assert store.add_triples([t]) == 0         # 库内已存在
    assert store.count() == 1
    # 同 (s,o) 不同谓词是另一条；字段不全的条目被丢弃
    assert store.add_triples([{**t, "predicate": "关联"}, {"subject": "x", "predicate": ""}]) == 1
    assert store.count() == 2


def test_store_restart_persistence(graph_env):
    make_repo, run = graph_env
    store = GraphStore(make_repo(), runner=run, seed=False)
    store.add_triples([Triple("GMV", "计算自", "订单项价格")])

    # 全新 engine 打开同一 sqlite 文件（重启）：数据仍在，表非空 → seed=True 也不灌种
    store2 = GraphStore(make_repo(), runner=run, seed=True)
    assert store2.count() == 1
    assert store2.list_triples()[0] == {
        "subject": "GMV", "predicate": "计算自", "object": "订单项价格", "source": "manual"
    }
    assert store2.graph.has_edge("GMV", "订单项价格")


def test_store_graph_mirror_invalidation(graph_env):
    make_repo, run = graph_env
    store = GraphStore(make_repo(), runner=run, seed=False)
    store.add_triples([Triple("A", "p", "B")])

    g1 = store.graph
    assert g1.has_edge("A", "B")
    assert store.graph is g1                       # 未写入 → 复用同一镜像

    store.add_triples([Triple("B", "q", "C")])     # 有效写入 → 失效
    g2 = store.graph
    assert g2 is not g1
    assert g2.has_edge("B", "C") and g2.has_edge("A", "B")

    store.add_triples([Triple("B", "q", "C")])     # 幂等写入（0 新增）→ 不重建
    assert store.graph is g2


# ========== 种子：首启灌种 + 幂等 + 与术语库对齐 ==========

def test_seed_on_empty_table_and_idempotent(graph_env):
    make_repo, run = graph_env
    store = GraphStore(make_repo(), runner=run, seed=True)
    assert store.count() == len(SEED_TRIPLES)
    assert all(t["source"] == "seed" for t in store.list_triples())

    # 二次启动（重启）：表非空 → 不重复灌种
    store2 = GraphStore(make_repo(), runner=run, seed=True)
    assert store2.count() == len(SEED_TRIPLES)

    # 种子与 terminology 种子口径对齐：三大指标实体都在图上且可溯源
    g = store2.graph
    assert {"GMV", "复购率", "客单价"} <= set(g.nodes)
    assert g.has_edge("GMV", "订单项价格")


# ========== service：查询原语 ==========

@pytest.fixture
def small_graph(graph_env):
    """六条边的已知小图（含正反向路径），供 query/path 断言精确结构。"""
    make_repo, run = graph_env
    store = GraphStore(make_repo(), runner=run, seed=False)
    store.add_triples([
        Triple("订单", "属于", "客户"),
        Triple("订单项", "属于", "订单"),
        Triple("GMV", "计算自", "订单项价格"),
        Triple("订单项价格", "属于", "订单项"),
        Triple("复购率", "统计自", "客户唯一标识"),
        Triple("客户唯一标识", "属于", "客户"),
    ])
    return store, GraphService(store)


def test_query_entity_depth_and_direction(small_graph):
    _, svc = small_graph

    # depth=1：出边+入边都算邻居，方向按真实三元组返回
    res = svc.query_entity("订单")
    assert res["nodes"] == sorted({"订单", "客户", "订单项"})
    assert {"subject": "订单", "predicate": "属于", "object": "客户", "source": "manual"} in res["edges"]
    assert {"subject": "订单项", "predicate": "属于", "object": "订单", "source": "manual"} in res["edges"]
    assert len(res["edges"]) == 2

    # depth=2：多扩一跳（订单项价格 / 客户唯一标识 进来）；GMV 距订单 3 跳，不包含
    res2 = svc.query_entity("订单", depth=2)
    assert {"订单项价格", "客户唯一标识"} <= set(res2["nodes"])
    assert "GMV" not in res2["nodes"]
    assert len(res2["edges"]) == 4

    # 不存在实体 → None
    assert svc.query_entity("不存在") is None


def test_find_path_predicate_chain(small_graph):
    store, svc = small_graph

    # 全正向链：GMV → 客户
    res = svc.find_path("GMV", "客户")
    assert res["found"] is True and res["hops"] == 4
    assert res["path"] == ["GMV", "订单项价格", "订单项", "订单", "客户"]
    assert [e["predicate"] for e in res["edges"]] == ["计算自", "属于", "属于", "属于"]
    assert res["chain"] == "GMV -[计算自]-> 订单项价格 -[属于]-> 订单项 -[属于]-> 订单 -[属于]-> 客户"

    # 含反向跳：复购率 → 订单（末跳客户←订单是入边，链上标 <-，edges 保留真实方向）
    res2 = svc.find_path("复购率", "订单")
    assert res2["found"] is True
    assert res2["path"] == ["复购率", "客户唯一标识", "客户", "订单"]
    assert res2["chain"].endswith("客户 <-[属于]- 订单")
    assert res2["edges"][-1] == {"subject": "订单", "predicate": "属于", "object": "客户"}

    # 无路可达（孤岛）与端点缺失
    store.add_triples([Triple("孤岛A", "关联", "孤岛B")])
    res3 = svc.find_path("GMV", "孤岛A")
    assert res3["found"] is False and res3["missing"] == []
    res4 = svc.find_path("GMV", "不存在")
    assert res4["found"] is False and res4["missing"] == ["不存在"]


def test_service_extract_and_add_and_stats(graph_env):
    make_repo, run = graph_env
    store = GraphStore(make_repo(), runner=run, seed=False)
    llm = FakeLLM('[{"subject": "评分", "predicate": "属于", "object": "评价"}]')
    svc = GraphService(store, llm_provider=lambda: llm)

    out = svc.extract_and_add("评分属于评价")
    assert out["added"] == 1
    assert out["triples"][0] == {"subject": "评分", "predicate": "属于", "object": "评价", "source": "llm"}
    assert store.count() == 1

    # 空文本：不调 LLM 直接空结果
    assert svc.extract_and_add("  ") == {"triples": [], "added": 0}
    assert llm.calls == 1

    stats = svc.stats()
    assert stats["entity_count"] == 2 and stats["triple_count"] == 1
    assert stats["predicates"] == {"属于": 1} and stats["sources"] == {"llm": 1}

    # 未配置 LLM → 显式失败（不静默返回空）
    with pytest.raises(ValueError, match="LLM"):
        GraphService(store).extract_and_add("有内容的文本")


# ========== graph_search 门控工具 ==========

def test_graph_search_tool_output(graph_env):
    make_repo, run = graph_env
    svc = GraphService(GraphStore(make_repo(), runner=run, seed=True))
    tool = create_graph_search_tool(svc)
    assert tool.name == "graph_search"

    # 文本化子图：每行 `主语 -[谓词]-> 宾语`
    out = tool.invoke({"entity": "GMV"})
    assert "GMV -[计算自]-> 订单项价格" in out
    assert "GMV -[按月分组于]-> 下单时间" in out

    # 未精确命中 → 相近实体提示（"单价" 是 "客单价" 的子串）
    out2 = tool.invoke({"entity": "单价"})
    assert "不存在" in out2 and "客单价" in out2

    # 毫无相近实体
    out3 = tool.invoke({"entity": "航天飞机"})
    assert "不存在" in out3


# ========== API：TestClient + overrides ==========

@pytest.fixture
def client(graph_env):
    from app.core.dependencies import get_graph_service
    from app.main import app

    make_repo, run = graph_env
    store = GraphStore(make_repo(), runner=run, seed=True)
    svc = GraphService(
        store,
        llm_provider=lambda: FakeLLM(
            '[{"subject": "发货时长", "predicate": "计算自", "object": "发货时间"}]'
        ),
    )
    app.dependency_overrides[get_graph_service] = lambda: svc
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_graph_full_flow(client):
    # stats：种子已入库
    body = client.get("/api/graph/stats").json()
    assert body["triple_count"] == len(SEED_TRIPLES)
    assert body["sources"] == {"seed": len(SEED_TRIPLES)}

    # 手动添加（一条与种子重复 + 一条新）：幂等
    resp = client.post("/api/graph/triples", json={"triples": [
        {"subject": "订单", "predicate": "属于", "object": "客户"},
        {"subject": "退货单", "predicate": "属于", "object": "订单"},
    ]})
    assert resp.status_code == 201
    assert resp.json()["added"] == 1 and resp.json()["skipped"] == 1
    assert resp.json()["total"] == len(SEED_TRIPLES) + 1

    # 空列表被 pydantic 拒绝
    assert client.post("/api/graph/triples", json={"triples": []}).status_code == 422

    # LLM 抽取（假模型注入）
    resp = client.post("/api/graph/extract", json={"text": "发货时长计算自发货时间"})
    assert resp.status_code == 200
    assert resp.json()["added"] == 1
    assert resp.json()["triples"][0]["subject"] == "发货时长"

    # 实体邻居子图
    resp = client.get("/api/graph/entity/GMV")
    assert resp.status_code == 200
    assert any(e["object"] == "订单项价格" for e in resp.json()["edges"])
    assert client.get("/api/graph/entity/不存在").status_code == 404

    # 最短路（含谓词链）
    resp = client.get("/api/graph/path", params={"from": "GMV", "to": "客户"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["path"][0] == "GMV" and body["path"][-1] == "客户"
    assert "-[" in body["chain"]
    assert client.get("/api/graph/path", params={"from": "GMV", "to": "没有这个"}).status_code == 404


# ========== SKILL.md：可解析 + 依赖展开含 graph_search ==========

async def test_knowledge_graph_skill_declares_graph_search(skill_service):
    skill = await skill_service.get_skill("knowledge-graph")
    assert skill is not None
    assert "graph_search" in skill.get_tools()
    assert "口径溯源" in skill.parsed.body  # 正文写了何时用图谱

    expanded = await skill_service.expand_dependencies(["knowledge-graph"])
    assert "graph_search" in expanded.tools
    # 激活 knowledge-graph 后，graph_search 应在其直接声明的工具集中（门控解锁依据）
    assert "graph_search" in expanded.tools_of({"knowledge-graph"})
