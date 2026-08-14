"""实体 Embedding 索引。

默认使用进程内派生索引，保证 Milvus 未部署时图谱仍可用；打开
``graph_entity_milvus_enabled`` 后使用独立 ``graph_entities`` collection。SQLite
实体表始终是真相源，索引写入失败只标记待重建，不回滚关系写入。
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from typing import Sequence

from loguru import logger

from app.graph.entities import EntityCandidate, cosine_similarity, embedding_hash, entity_index_text
from app.graph.scope import GraphScope


class GraphEntityIndex:
    def __init__(self, settings, embed_service=None, embed_fn=None):
        self.settings = settings
        self._embedding_service = embed_service
        self._embed_fn = embed_fn
        self._vectors: OrderedDict[tuple[str, int, str], list[float]] = OrderedDict()
        self._vector_cache_size = max(1, int(getattr(settings, "graph_entity_vector_cache_size", 10000)))
        self._collection = None
        self._milvus_ready = False
        # GraphService 既可能从 FastAPI 主循环也可能从 run_sync 后台循环访问；
        # 不使用绑定单 event loop 的 asyncio.Lock。
        self._milvus_lock = threading.Lock()

    async def _embed(self, text: str) -> list[float]:
        if self._embed_fn is not None:
            result = self._embed_fn(text)
            if hasattr(result, "__await__"):
                result = await result
            return list(result or [])
        if self._embedding_service is None:
            from app.rag.embeddings import EmbeddingService

            self._embedding_service = EmbeddingService(self.settings)
        return await self._embedding_service.embed_text(text)

    async def _ensure_milvus(self, dimension: int | None = None) -> bool:
        if not getattr(self.settings, "graph_entity_milvus_enabled", False):
            return False
        return await asyncio.to_thread(self._ensure_milvus_sync, dimension)

    def _ensure_milvus_sync(self, dimension: int | None = None) -> bool:
        """在工作线程执行 pymilvus 的同步连接/建索引操作。"""
        if self._milvus_ready:
            return True
        with self._milvus_lock:
            if self._milvus_ready:
                return True
            try:
                from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility

                if not connections.has_connection("graph_entities"):
                    connections.connect(
                        alias="graph_entities",
                        host=self.settings.milvus_host,
                        port=self.settings.milvus_port,
                    )
                name = self.settings.graph_entity_collection
                dimension = dimension or getattr(self.settings, "graph_entity_embedding_dim", 1024)
                if utility.has_collection(name, using="graph_entities"):
                    self._collection = Collection(name, using="graph_entities")
                else:
                    fields = [
                        FieldSchema(name="entity_id", dtype=DataType.INT64, is_primary=True, auto_id=False),
                        FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dimension),
                        FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
                        FieldSchema(name="metadata", dtype=DataType.JSON),
                    ]
                    schema = CollectionSchema(fields=fields, description="作用域知识图谱实体向量")
                    self._collection = Collection(name=name, schema=schema, using="graph_entities")
                    self._collection.create_index(
                        field_name="vector",
                        index_params={"metric_type": "IP", "index_type": "IVF_FLAT", "params": {"nlist": 128}},
                    )
                self._collection.load()
                self._milvus_ready = True
                return True
            except Exception as exc:  # noqa: BLE001 - 增强索引失败必须降级
                logger.warning(f"图谱实体 Milvus 索引不可用，回退进程内索引: {exc}")
                self._milvus_ready = False
                return False

    def _cache_vector(self, key: tuple[str, int, str], vector: list[float]) -> None:
        self._vectors[key] = vector
        self._vectors.move_to_end(key)
        while len(self._vectors) > self._vector_cache_size:
            self._vectors.popitem(last=False)

    async def _upsert_with_status(self, entities: Sequence[dict], scope: GraphScope) -> dict[int, str]:
        """生成并写入实体向量，返回成功实体及其内容 hash。

        返回 hash 供 GraphService 回写 ``embedding_status=synced``；失败实体
        保持 pending，下一次显式写入或重建时可重试。
        """
        if not entities:
            return {}
        vectors: list[list[float]] = []
        rows: list[dict] = []
        indexed: dict[int, str] = {}
        for entity in entities:
            text = entity_index_text(entity)
            vector_hash = embedding_hash(text)
            try:
                vector = await self._embed(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"实体向量化失败，实体 {entity.get('canonical_name')}: {exc}")
                continue
            if not vector:
                continue
            vectors.append(vector)
            rows.append(entity)
            indexed[int(entity["id"])] = vector_hash
            self._cache_vector((scope.key, int(entity["id"]), vector_hash), vector)
        if not rows:
            return {}
        if await self._ensure_milvus(len(vectors[0])):
            try:
                ids = [int(row["id"]) for row in rows]
                await asyncio.to_thread(
                    self._upsert_milvus_sync,
                    ids,
                    vectors,
                    [entity_index_text(row) for row in rows],
                    [
                        {
                            "scope_key": scope.key,
                            "workspace_id": scope.workspace_id,
                            "datasource_id": scope.datasource_id,
                            "canonical_name": row.get("canonical_name", ""),
                            "entity_type": row.get("entity_type", "unknown"),
                            "embedding_hash": indexed[int(row["id"])],
                        }
                        for row in rows
                    ],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"实体向量写入 Milvus 失败，保留进程内索引: {exc}")
        return indexed

    async def upsert(self, entities: Sequence[dict], scope: GraphScope) -> int:
        """兼容旧调用方：返回成功写入的实体数。"""

        return len(await self._upsert_with_status(entities, scope))

    async def upsert_with_status(self, entities: Sequence[dict], scope: GraphScope) -> dict[int, str]:
        """返回成功实体及 hash，供持久化层回写 Embedding 状态。"""

        return await self._upsert_with_status(entities, scope)

    def _upsert_milvus_sync(self, ids, vectors, contents, metadata) -> None:
        self._collection.delete(f"entity_id in {ids}")
        self._collection.insert([ids, vectors, contents, metadata])
        self._collection.flush()

    async def search(
        self,
        query: str,
        entities: Sequence[dict],
        scope: GraphScope,
        top_k: int = 5,
    ) -> list[EntityCandidate]:
        if not entities:
            return []
        try:
            query_vector = await self._embed(query)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"实体查询向量化失败，回退 lexical: {exc}")
            return []
        if not query_vector:
            return []

        # Milvus 是可选的持久化索引。先查它可以复用重启前的向量；metadata
        # 在应用侧做作用域过滤，避免依赖不同 Milvus 版本的 JSON expr 语法。
        if await self._ensure_milvus(len(query_vector)):
            try:
                hits = await asyncio.to_thread(
                    self._collection.search,
                    [query_vector],
                    "vector",
                    {"metric_type": "IP", "params": {"nprobe": 10}},
                    max(1, min(int(top_k) * 5, 100)),
                    output_fields=["metadata"],
                )
                by_id = {int(entity["id"]): entity for entity in entities}
                candidates: list[EntityCandidate] = []
                for hit in hits[0] if hits else []:
                    entity = by_id.get(int(hit.id))
                    if entity is None:
                        continue
                    metadata = getattr(hit, "entity", None)
                    metadata = metadata.get("metadata", {}) if metadata else {}
                    if metadata and metadata.get("scope_key") not in (None, scope.key):
                        continue
                    candidates.append(
                        EntityCandidate(entity=entity, score=float(hit.distance), method="embedding:milvus")
                    )
                if candidates:
                    return candidates[: max(1, min(int(top_k), 20))]
            except Exception as exc:  # noqa: BLE001 - 持久化索引失败必须回退本地
                logger.warning(f"实体 Milvus 查询失败，回退进程内索引: {exc}")

        # 先用本地缓存覆盖测试和小图谱；Milvus 仍在写入时作为持久化索引。
        scored: list[EntityCandidate] = []
        for entity in entities:
            text = entity_index_text(entity)
            key = (scope.key, int(entity["id"]), embedding_hash(text))
            vector = self._vectors.get(key)
            if vector is None:
                try:
                    vector = await self._embed(text)
                except Exception:
                    return []
                if not vector:
                    continue
                self._cache_vector(key, vector)
            else:
                self._vectors.move_to_end(key)
            score = cosine_similarity(query_vector, vector)
            scored.append(EntityCandidate(entity=entity, score=score, method="embedding"))
        scored.sort(key=lambda item: (-item.score, item.entity.get("canonical_name", "")))
        return scored[: max(1, min(int(top_k), 20))]
