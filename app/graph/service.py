"""知识图谱 - 服务编排（作用域 / 抽取入库 / 实体解析 / 路径 / 统计）

Yuxi 对应物是 MilvusGraphService（yuxi-reference/backend/package/yuxi/knowledge/graphs/
milvus_graph_service.py）：Neo4j Cypher 子图查询 + Milvus 实体向量召回 + PPR 重排 chunk。
本轻量版把「图查询」收敛成 NetworkX 上的两个原语：

- query_entity：以实体为中心的 depth 跳邻居子图（出边入边都带谓词）
- find_path：两实体最短路 —— **可达性按无向算**（业务问句"A 和 B 有什么关系"
  不关心方向），但每一跳都保留三元组的真实方向与谓词（正向 -[p]-> / 反向 <-[p]-），
  谓词链因此可直接转述成口径溯源的解释。

请求级 GraphScope 不进入 Agent 工具参数，由 API、Chat、Analysis 和 worker 设置后由
本服务读取。实体解析遵循规范名 → 别名 → Embedding/词法候选顺序；Embedding 只是
增强路径，失败时回退到可解释的精确/别名/子串匹配。LLM 惰性注入：llm_provider 是
零参工厂（如 LLMFactory.create_llm），首次抽取才调用，不触发 /extract 就不要求
配置 LLM_API_KEY。
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

import networkx as nx

from app.graph.extractor import Triple, extract_triples
from app.graph.resolver import EntityResolver
from app.graph.scope import GraphScope, current_graph_scope
from app.graph.store import GraphStore


class GraphService:
    """图谱门面：路由与 graph_search 工具都只依赖本类，不直接碰 store/extractor。"""

    def __init__(
        self,
        store: GraphStore,
        llm_provider: Optional[Callable[[], Any]] = None,
        entity_resolver: EntityResolver | None = None,
        embed_fn: Callable | None = None,
        scope: GraphScope | None = None,
    ):
        self._store = store
        self._llm_provider = llm_provider
        self._llm: Any = None
        self._default_scope = scope or GraphScope()
        self._entity_resolver = entity_resolver
        self._embed_fn = embed_fn

    def _scope(self) -> GraphScope:
        return current_graph_scope() or self._default_scope

    def _resolver(self) -> EntityResolver:
        if self._entity_resolver is None:
            from app.core.settings import settings

            entity_index = None
            if self._embed_fn is None and getattr(settings, "graph_entity_embedding_enabled", False):
                if settings.embedding_provider == "bge" or settings.embedding_api_key:
                    from app.graph.entity_index import GraphEntityIndex

                    entity_index = GraphEntityIndex(settings)

            self._entity_resolver = EntityResolver(
                self._store.entity_repo,
                self._store._run,
                embed_fn=self._embed_fn,
                index=entity_index,
                top_k=getattr(settings, "graph_entity_top_k", 5),
                min_score=getattr(settings, "graph_entity_min_score", 0.72),
                merge_margin=getattr(settings, "graph_entity_merge_margin", 0.05),
            )
        return self._entity_resolver

    # ========== 写入 ==========

    def add_triples(self, triples: Sequence[Triple | dict]) -> dict:
        """手动/批量入库（幂等）。skipped = 重复或字段不全被跳过的条数。"""
        scope = self._scope()
        added = self._store.add_triples(triples, scope=scope)
        self._maybe_index_entities(scope)
        return {
            "added": added,
            "skipped": len(triples) - added,
            "total": self._store.count(scope),
            "scope": {"key": scope.key, "workspace_id": scope.workspace_id, "datasource_id": scope.datasource_id},
        }

    def extract_and_add(self, text: str) -> dict:
        """LLM 从文本抽取三元组并入库；空文本不调 LLM 直接返回空结果。"""
        if not text or not text.strip():
            return {"triples": [], "added": 0}
        triples = extract_triples(text, self._get_llm())
        added = self._store.add_triples(triples, scope=self._scope()) if triples else 0
        self._maybe_index_entities(self._scope())
        return {"triples": [t.to_dict() for t in triples], "added": added, "scope": self._scope().key}

    def _maybe_index_entities(self, scope: GraphScope) -> None:
        """在关系事务完成后尽力更新实体索引，索引失败不回滚 SQLite 事实。"""

        try:
            from app.core.settings import settings

            if not getattr(settings, "graph_entity_embedding_enabled", False) or not self._store.entity_repo:
                return
            index = self._resolver()._index
            if index is None:
                return
            entities = self._store._run(self._store.entity_repo.list_scope(scope.key))
            self._store._run(index.upsert(entities, scope))
        except Exception:
            # GraphEntityIndex 自身已对单实体/ Milvus 失败做降级；这里再兜底一次，
            # 确保外部 Embedding 服务故障不会让关系写入接口失败。
            return

    def sync_catalog(self, catalog: dict) -> dict:
        """把已审核数据源目录同步为表/字段实体和外键关系。

        这是显式操作，不挂在上传或审核保存主链路上；未审核语义不会进入属性。
        """

        scope = self._scope()
        entities: list[dict] = []
        triples: list[dict] = []
        for table in catalog.get("tables", []) if isinstance(catalog, dict) else []:
            schema = str(table.get("schema_name") or "").strip()
            table_name = str(table.get("table_name") or "").strip()
            if not table_name:
                continue
            qualified_table = f"{schema}.{table_name}" if schema else table_name
            table_approved = table.get("review_status") == "approved"
            entities.append(
                {
                    "canonical_name": qualified_table,
                    "scope_key": scope.key,
                    "workspace_id": scope.workspace_id,
                    "datasource_id": scope.datasource_id,
                    "entity_type": "table",
                    "aliases": [table_name],
                    "attributes": {
                        "comment": (
                            table.get("reviewed_comment") if table_approved else table.get("physical_comment", "")
                        ),
                        "table_type": table.get("table_type", "table"),
                    },
                    "source": "schema_reviewed" if table_approved else "physical_schema",
                }
            )
            for column in table.get("columns", []) or []:
                column_name = str(column.get("column_name") or "").strip()
                if not column_name:
                    continue
                qualified_column = f"{qualified_table}.{column_name}"
                column_approved = column.get("review_status") == "approved"
                aliases = [column_name]
                if column_approved:
                    aliases.extend(column.get("reviewed_synonyms") or [])
                entities.append(
                    {
                        "canonical_name": qualified_column,
                        "scope_key": scope.key,
                        "workspace_id": scope.workspace_id,
                        "datasource_id": scope.datasource_id,
                        "entity_type": "column",
                        "aliases": aliases,
                        "attributes": {
                            "comment": column.get("reviewed_comment")
                            if column_approved
                            else column.get("physical_comment", ""),
                            "data_type": column.get("data_type", "UNKNOWN"),
                            "primary_key": bool(column.get("primary_key")),
                        },
                        "source": "schema_reviewed" if column_approved else "physical_schema",
                    }
                )
                triples.append(
                    {
                        "subject": qualified_column,
                        "predicate": "属于",
                        "object": qualified_table,
                        "source": "schema_reviewed" if column_approved and table_approved else "physical_schema",
                        "source_ref": f"datasource:{scope.datasource_id}",
                    }
                )
                reference = column.get("references") or {}
                target_table = str(reference.get("table") or "").strip()
                target_column = str(reference.get("column") or "").strip()
                if target_table and target_column:
                    target_schema = str(reference.get("schema") or schema).strip()
                    target = f"{target_schema}.{target_table}" if target_schema else target_table
                    triples.append(
                        {
                            "subject": qualified_column,
                            "predicate": "引用",
                            "object": f"{target}.{target_column}",
                            "source": "physical_schema",
                            "source_ref": f"datasource:{scope.datasource_id}",
                        }
                    )
        if self._store.entity_repo and entities:
            self._store._run(self._store.entity_repo.upsert_many(entities))
            self._maybe_index_entities(scope)
        added = self._store.add_triples(triples, scope=scope) if triples else 0
        return {"scope": scope.key, "entities": len(entities), "triples_added": added}

    def _get_llm(self) -> Any:
        if self._llm is None:
            if self._llm_provider is None:
                raise ValueError("GraphService 未配置 LLM（llm_provider），无法执行抽取")
            self._llm = self._llm_provider()
        return self._llm

    # ========== 查询 ==========

    def query_entity(self, name: str, depth: int = 1, scope: GraphScope | None = None) -> Optional[dict]:
        """实体的 depth 跳邻居子图；实体不存在返回 None（路由层转 404）。

        邻居按无向可达收集（undirected=True：入边邻居也是邻居），返回这些节点间的
        全部边，方向按真实三元组给出 (subject, predicate, object)。
        """
        scope = scope or self._scope()
        g = self._store.graph_for(scope)
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
        return {"entity": name, "depth": depth, "nodes": sorted(ego.nodes), "edges": edges, "scope": scope.key}

    def find_path(
        self,
        source: str,
        target: str,
        scope: GraphScope | None = None,
        max_hops: int | None = None,
    ) -> dict:
        """两实体间最短路（无向可达）；chain 为可读谓词链，每跳标注真实方向。

        返回 dict：found / missing（图上不存在的端点，路由层转 404）/
        hops / path（节点序列）/ edges（真实方向三元组）/ chain（如
        `GMV -[计算自]-> 订单项价格 -[属于]-> 订单项`；反向边渲染为 `A <-[p]- B`）。
        """
        scope = scope or self._scope()
        g = self._store.graph_for(scope)
        result = {
            "from": source,
            "to": target,
            "found": False,
            "missing": [],
            "hops": 0,
            "path": [],
            "edges": [],
            "chain": "",
            "scope": scope.key,
        }
        result["missing"] = [n for n in dict.fromkeys((source, target)) if n not in g]
        if result["missing"]:
            return result

        try:
            nodes = nx.shortest_path(g.to_undirected(as_view=True), source, target)
        except nx.NetworkXNoPath:
            return result

        if max_hops is not None and len(nodes) - 1 > max(1, int(max_hops)):
            return result

        edges: list[dict] = []
        chain_parts: list[str] = [source]
        for u, v in zip(nodes, nodes[1:]):
            if g.has_edge(u, v):  # 正向：u -[p]-> v
                predicate = sorted(g[u][v])[0]
                data = g[u][v][predicate]
                edges.append(
                    {
                        "subject": u,
                        "predicate": predicate,
                        "object": v,
                        "source": data.get("source", ""),
                        "source_ref": data.get("source_ref"),
                        "provenance": data.get("provenance") or {},
                    }
                )
                chain_parts.append(f"-[{predicate}]-> {v}")
            else:  # 只有反向边 v -[p]-> u：链上渲染为 u <-[p]- v，edges 保留真实方向
                predicate = sorted(g[v][u])[0]
                data = g[v][u][predicate]
                edges.append(
                    {
                        "subject": v,
                        "predicate": predicate,
                        "object": u,
                        "source": data.get("source", ""),
                        "source_ref": data.get("source_ref"),
                        "provenance": data.get("provenance") or {},
                    }
                )
                chain_parts.append(f"<-[{predicate}]- {v}")

        result.update(found=True, hops=len(nodes) - 1, path=list(nodes), edges=edges, chain=" ".join(chain_parts))
        return result

    async def find_path_resolved(self, source: str, target: str, max_hops: int = 3) -> dict:
        """先解析自然语言实体，再执行作用域内确定性路径查询。"""

        scope = self._scope()
        resolver = self._resolver()
        source_result, target_result = await _gather_resolutions(
            resolver.resolve(source, scope), resolver.resolve(target, scope)
        )
        if source_result.status == "ambiguous" or target_result.status == "ambiguous":
            return {
                "status": "ambiguous",
                "found": False,
                "from": source,
                "to": target,
                "candidates": {
                    "from": _candidate_dicts(source_result.candidates),
                    "to": _candidate_dicts(target_result.candidates),
                },
                "scope": scope.key,
            }
        missing = []
        if source_result.entity is None:
            missing.append(source)
        if target_result.entity is None:
            missing.append(target)
        if missing:
            return {
                "status": "missing",
                "found": False,
                "missing": missing,
                "from": source,
                "to": target,
                "scope": scope.key,
            }

        # find_path 是同步的 SQLite/NetworkX 读取；路径工具运行在异步
        # Agent 链路中时放到线程，避免再次在运行中的 event loop 内嵌套
        # run_until_complete。
        import asyncio

        result = await asyncio.to_thread(
            self.find_path,
            source_result.entity["canonical_name"],
            target_result.entity["canonical_name"],
            scope=scope,
            max_hops=max_hops,
        )
        result["status"] = "found" if result["found"] else "unreachable"
        result["resolution"] = {
            "from": _resolution_dict(source_result),
            "to": _resolution_dict(target_result),
        }
        return result

    def suggest_entities(self, keyword: str, limit: int = 5) -> list[str]:
        """按子串（大小写不敏感）给出相近实体名，供 graph_search 未命中时提示重查。"""
        kw = (keyword or "").strip().lower()
        if not kw:
            return []
        g = self._store.graph_for(self._scope())
        return sorted(n for n in g.nodes if kw in str(n).lower())[:limit]

    def merge_entities(self, survivor_id: int, duplicate_id: int) -> dict:
        """管理员显式合并实体；属性和关系由仓储在同一事务中处理。"""

        return self._store.merge_entities(survivor_id, duplicate_id, scope=self._scope())

    def stats(self) -> dict:
        """图谱统计：实体数 / 三元组数 / 谓词分布 / 来源分布。"""
        predicates: dict[str, int] = {}
        sources: dict[str, int] = {}
        scope = self._scope()
        for t in self._store.list_triples(scope):
            predicates[t["predicate"]] = predicates.get(t["predicate"], 0) + 1
            sources[t["source"]] = sources.get(t["source"], 0) + 1
        g = self._store.graph_for(scope)
        return {
            "entity_count": g.number_of_nodes(),
            "triple_count": g.number_of_edges(),
            "predicates": dict(sorted(predicates.items(), key=lambda kv: (-kv[1], kv[0]))),
            "sources": sources,
            "scope": scope.key,
        }


def _candidate_dicts(candidates) -> list[dict]:
    return [
        {
            "entity": candidate.entity.get("canonical_name"),
            "entity_type": candidate.entity.get("entity_type"),
            "score": candidate.score,
            "method": candidate.method,
        }
        for candidate in candidates
    ]


def _resolution_dict(result) -> dict:
    return {
        "input": result.input_name,
        "resolved": result.entity.get("canonical_name") if result.entity else None,
        "method": result.method,
        "score": result.score,
    }


async def _gather_resolutions(source_awaitable, target_awaitable):
    # 使用 asyncio.gather 让两个端点的 Embedding 请求并行，精确/别名命中时仍然
    # 只是轻量的后台 DB 查询。
    import asyncio

    return await asyncio.gather(source_awaitable, target_awaitable)
