"""知识图谱 - 服务编排（抽取入库 / 邻居子图 / 最短路 / 统计）

Yuxi 对应物是 MilvusGraphService（yuxi-reference/backend/package/yuxi/knowledge/graphs/
milvus_graph_service.py）：Neo4j Cypher 子图查询 + Milvus 实体向量召回 + PPR 重排 chunk。
本轻量版把「图查询」收敛成 NetworkX 上的两个原语：

- query_entity：以实体为中心的 depth 跳邻居子图（出边入边都带谓词）
- find_path：两实体最短路 —— **可达性按无向算**（业务问句"A 和 B 有什么关系"
  不关心方向），但每一跳都保留三元组的真实方向与谓词（正向 -[p]-> / 反向 <-[p]-），
  谓词链因此可直接转述成口径溯源的解释。

LLM 惰性注入：llm_provider 是零参工厂（如 LLMFactory.create_llm），首次抽取才调用，
不触发 /extract 就不要求配置 LLM_API_KEY（与 llm.py 的显式失败设计一致）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

import networkx as nx

from app.graph.extractor import Triple, extract_triples
from app.graph.store import GraphStore


class GraphService:
    """图谱门面：路由与 graph_search 工具都只依赖本类，不直接碰 store/extractor。"""

    def __init__(self, store: GraphStore, llm_provider: Optional[Callable[[], Any]] = None):
        self._store = store
        self._llm_provider = llm_provider
        self._llm: Any = None

    # ========== 写入 ==========

    def add_triples(self, triples: Sequence[Triple | dict]) -> dict:
        """手动/批量入库（幂等）。skipped = 重复或字段不全被跳过的条数。"""
        added = self._store.add_triples(triples)
        return {"added": added, "skipped": len(triples) - added, "total": self._store.count()}

    def extract_and_add(self, text: str) -> dict:
        """LLM 从文本抽取三元组并入库；空文本不调 LLM 直接返回空结果。"""
        if not text or not text.strip():
            return {"triples": [], "added": 0}
        triples = extract_triples(text, self._get_llm())
        added = self._store.add_triples(triples) if triples else 0
        return {"triples": [t.to_dict() for t in triples], "added": added}

    def _get_llm(self) -> Any:
        if self._llm is None:
            if self._llm_provider is None:
                raise ValueError("GraphService 未配置 LLM（llm_provider），无法执行抽取")
            self._llm = self._llm_provider()
        return self._llm

    # ========== 查询 ==========

    def query_entity(self, name: str, depth: int = 1) -> Optional[dict]:
        """实体的 depth 跳邻居子图；实体不存在返回 None（路由层转 404）。

        邻居按无向可达收集（undirected=True：入边邻居也是邻居），返回这些节点间的
        全部边，方向按真实三元组给出 (subject, predicate, object)。
        """
        g = self._store.graph
        if name not in g:
            return None
        depth = max(1, int(depth))
        # ego_graph 返回半径内节点的诱导子图（含节点间全部边与边属性）
        ego = nx.ego_graph(g, name, radius=depth, undirected=True)
        edges = [
            {"subject": u, "predicate": data.get("predicate", ""), "object": v, "source": data.get("source", "")}
            for u, v, data in ego.edges(data=True)
        ]
        edges.sort(key=lambda e: (e["subject"], e["predicate"], e["object"]))
        return {"entity": name, "depth": depth, "nodes": sorted(ego.nodes), "edges": edges}

    def find_path(self, source: str, target: str) -> dict:
        """两实体间最短路（无向可达）；chain 为可读谓词链，每跳标注真实方向。

        返回 dict：found / missing（图上不存在的端点，路由层转 404）/
        hops / path（节点序列）/ edges（真实方向三元组）/ chain（如
        `GMV -[计算自]-> 订单项价格 -[属于]-> 订单项`；反向边渲染为 `A <-[p]- B`）。
        """
        g = self._store.graph
        result = {
            "from": source,
            "to": target,
            "found": False,
            "missing": [],
            "hops": 0,
            "path": [],
            "edges": [],
            "chain": "",
        }
        result["missing"] = [n for n in dict.fromkeys((source, target)) if n not in g]
        if result["missing"]:
            return result

        try:
            nodes = nx.shortest_path(g.to_undirected(as_view=True), source, target)
        except nx.NetworkXNoPath:
            return result

        edges: list[dict] = []
        chain_parts: list[str] = [source]
        for u, v in zip(nodes, nodes[1:]):
            if g.has_edge(u, v):  # 正向：u -[p]-> v
                predicate = next(iter(g[u][v]))  # edge key 即谓词；多谓词并存时取任一
                edges.append({"subject": u, "predicate": predicate, "object": v})
                chain_parts.append(f"-[{predicate}]-> {v}")
            else:  # 只有反向边 v -[p]-> u：链上渲染为 u <-[p]- v，edges 保留真实方向
                predicate = next(iter(g[v][u]))
                edges.append({"subject": v, "predicate": predicate, "object": u})
                chain_parts.append(f"<-[{predicate}]- {v}")

        result.update(found=True, hops=len(nodes) - 1, path=list(nodes), edges=edges, chain=" ".join(chain_parts))
        return result

    def suggest_entities(self, keyword: str, limit: int = 5) -> list[str]:
        """按子串（大小写不敏感）给出相近实体名，供 graph_search 未命中时提示重查。"""
        kw = (keyword or "").strip().lower()
        if not kw:
            return []
        g = self._store.graph
        return sorted(n for n in g.nodes if kw in str(n).lower())[:limit]

    def stats(self) -> dict:
        """图谱统计：实体数 / 三元组数 / 谓词分布 / 来源分布。"""
        predicates: dict[str, int] = {}
        sources: dict[str, int] = {}
        for t in self._store.list_triples():
            predicates[t["predicate"]] = predicates.get(t["predicate"], 0) + 1
            sources[t["source"]] = sources.get(t["source"], 0) + 1
        g = self._store.graph
        return {
            "entity_count": g.number_of_nodes(),
            "triple_count": g.number_of_edges(),
            "predicates": dict(sorted(predicates.items(), key=lambda kv: (-kv[1], kv[0]))),
            "sources": sources,
        }
