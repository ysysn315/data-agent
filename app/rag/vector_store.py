# 向量存储模块
# TODO: 任务 12.2 - 实现 VectorStore 类
# 向量存储模块
# TODO: 任务 12.2 - 实现 VectorStore 类

import asyncio
import json
import re
from collections import deque
from threading import RLock
from typing import Dict, Iterable, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from app.clients.milvus_client import MilvusClient
from app.rag.document_utils import document_key, document_source
from app.rag.embeddings import EmbeddingService
from app.rag.metadata_filters import (
    matches_metadata_filters,
    normalize_metadata_filters,
)


class VectorStore:
    _PERSISTED_OUTPUT_FIELDS = ["id", "content", "metadata"]

    def __init__(
        self,
        milvus_client: MilvusClient,
        embedding_service: EmbeddingService,
        reranker_llm: Optional[object] = None,
        reranker: Optional[object] = None,
        dense_top_k: int = 10,
        enable_rerank: bool = True,
        enable_hybrid: bool = True,
        max_bm25_documents: int = 50000,
        restore_batch_size: int = 1000,
    ):
        self.milvus = milvus_client
        self.embedding = embedding_service
        self.reranker_llm = reranker_llm
        self.reranker = reranker
        self.dense_top_k = dense_top_k
        self.enable_rerank = enable_rerank
        self.enable_hybrid = enable_hybrid
        self.max_bm25_documents = max(1, max_bm25_documents)
        self.restore_batch_size = max(1, restore_batch_size)
        self.bm25_retriever = None
        self.all_chunks: List[Dict] = []
        self._bm25_lock = RLock()
        logger.info("向量存储初始化完成")

    async def insert(self, chunks: List[Dict]) -> None:
        try:
            if not chunks:
                logger.warning("没有文档需要插入")
                return
            texts = [chunk["content"] for chunk in chunks]
            vectors = await self.embedding.embed_texts(texts)
            await asyncio.to_thread(self._insert_sync, vectors, chunks)
            logger.info(f"成功插入 {len(chunks)} 个文档块")

        except Exception as e:
            logger.error(f"插入文档失败: {str(e)}")
            raise Exception(f"插入文档失败: {str(e)}")

    def _insert_sync(self, vectors: List[List[float]], chunks: List[Dict]) -> None:
        """在线程中执行同步 Milvus 写入和 BM25 派生索引更新。"""
        texts = [chunk["content"] for chunk in chunks]
        data = [vectors, texts, [chunk["metadata"] for chunk in chunks]]
        self.milvus.collection.insert(data)
        self.milvus.collection.flush()
        if self.enable_hybrid:
            self._upsert_bm25_chunks(chunks)

    def _upsert_bm25_chunks(self, chunks: Iterable[Dict]) -> None:
        """把新写入的 chunk 合并进进程内 BM25 索引。

        Milvus 是持久化真相源，BM25 只是本地派生索引；这里仅维护内存副本，
        服务重启时由 ``restore_bm25_index`` 从 Milvus 重建。
        """
        with self._bm25_lock:
            merged_chunks = [*self.all_chunks, *chunks]
            if len(merged_chunks) > self.max_bm25_documents:
                merged_chunks = merged_chunks[-self.max_bm25_documents :]
                logger.warning(
                    "BM25 文档数超过上限，已保留最近 %s 个 chunk；Milvus 中的完整数据不受影响",
                    self.max_bm25_documents,
                )
            self._rebuild_bm25_sync(merged_chunks)

    @staticmethod
    def _normalize_metadata(value) -> Dict:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return dict(parsed) if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}

    @classmethod
    def _row_to_chunk(cls, row: Dict) -> Optional[Dict]:
        """把 Milvus 返回行转换为统一的 chunk 结构。"""
        content = row.get("content", "")
        if not content:
            return None
        return {
            "content": content,
            "metadata": cls._normalize_metadata(row.get("metadata")),
        }

    def _search_sync(self, query_vector: List[float], candidate_limit: int):
        """在线程中执行同步 Milvus 向量检索。"""
        return self.milvus.collection.search(
            data=[query_vector],
            anns_field="vector",
            param={"metric_type": "IP", "params": {"nprobe": 10}},
            limit=candidate_limit,
            output_fields=["content", "metadata"],
        )

    def _iter_persisted_chunks(self) -> Iterable[Dict]:
        """逐批读取 Milvus 中已有的文本块，供索引恢复和文档列表复用。"""
        collection = self.milvus.collection
        iterator = None
        try:
            if hasattr(collection, "query_iterator"):
                iterator = collection.query_iterator(
                    batch_size=self.restore_batch_size,
                    limit=-1,
                    # QueryIterator 需要主键推进游标；即使业务只用 content/metadata，
                    # 也必须把 id 放进返回字段。
                    output_fields=self._PERSISTED_OUTPUT_FIELDS,
                )
                while True:
                    batch = iterator.next()
                    # pymilvus 返回空列表结束；某些兼容客户端会返回 None。
                    if not batch:
                        break
                    for row in batch:
                        chunk = self._row_to_chunk(row)
                        if chunk is not None:
                            yield chunk
            else:
                # 兼容未提供 query_iterator 的旧版 pymilvus；仅作为回退路径。
                rows = collection.query(expr="id >= 0", output_fields=self._PERSISTED_OUTPUT_FIELDS)
                for row in rows:
                    chunk = self._row_to_chunk(row)
                    if chunk is not None:
                        yield chunk
        finally:
            if iterator is not None and hasattr(iterator, "close"):
                iterator.close()

    def _load_bm25_chunks_sync(self) -> List[Dict]:
        """同步读取 BM25 语料，并与增量写入保持“保留尾部”策略一致。"""
        return self._load_capped_chunks_sync()

    def _load_capped_chunks_sync(self) -> List[Dict]:
        """读取受上限约束的 chunk 子集，供 BM25 恢复和文档列表复用。"""
        chunks = deque(maxlen=self.max_bm25_documents)
        for chunk in self._iter_persisted_chunks():
            chunks.append(chunk)
        return list(chunks)

    def _rebuild_bm25_sync(self, chunks: List[Dict]) -> None:
        with self._bm25_lock:
            self.all_chunks = list(chunks)
            if not self.all_chunks:
                self.bm25_retriever = None
                return

            from app.rag.bm25 import BM25Retriever

            self.bm25_retriever = BM25Retriever()
            self.bm25_retriever.index(self.all_chunks)

    async def restore_bm25_index(self) -> int:
        """从 Milvus 恢复 BM25 语料，解决服务重启后只剩稠密检索的问题。

        Milvus 查询和 BM25 建索引都是同步操作，放到线程中避免阻塞事件循环；
        调用方仍可在单例初始化锁内等待恢复完成，确保返回的 VectorStore 可直接用于混合检索。
        """
        if not self.enable_hybrid:
            self.all_chunks = []
            self.bm25_retriever = None
            return 0

        chunks = await asyncio.to_thread(self._load_bm25_chunks_sync)
        await asyncio.to_thread(self._rebuild_bm25_sync, chunks)
        logger.info("BM25 索引恢复完成：%s 个 chunk", len(chunks))
        return len(chunks)

    def _list_documents_sync(self) -> List[Dict]:
        """同步扫描 Milvus 并聚合真实文档列表，避免在事件循环中阻塞。

        文档列表与 BM25 恢复使用同一个 chunk 上限，避免大库请求把全部文本块
        一次性读进内存；列表代表本次扫描保留的最近 chunk 所覆盖的 source。
        """
        chunks = self._load_capped_chunks_sync()

        documents: Dict[str, Dict] = {}
        for chunk in chunks:
            metadata = chunk.get("metadata") or {}
            source = document_source(chunk)
            if not source:
                continue
            item = documents.setdefault(
                source,
                {
                    "source": source,
                    "title": metadata.get("title") or source,
                    "doc_type": metadata.get("doc_type") or "text",
                    "chunk_count": 0,
                    "ingested_at": metadata.get("ingested_at") or metadata.get("timestamp"),
                    "sheet_names": set(),
                },
            )
            item["chunk_count"] += 1
            sheet_name = metadata.get("sheet_name")
            if sheet_name:
                item["sheet_names"].add(str(sheet_name))

        result = []
        for item in documents.values():
            item["sheet_names"] = sorted(item["sheet_names"])
            result.append(item)
        result.sort(key=lambda item: (item.get("ingested_at") or 0, item["source"]), reverse=True)
        return result

    async def list_documents(self) -> List[Dict]:
        """从 Milvus 聚合真实文档列表，而不是读取上传目录的临时文件。"""
        return await asyncio.to_thread(self._list_documents_sync)

    def _truncate(self, text: str, max_len: int = 260) -> str:
        text = (text or "").strip().replace("\n", " ")
        return text[:max_len]

    def _build_rerank_prompt(self, query: str, candidates: List[Dict]) -> str:
        lines = []
        for i, c in enumerate(candidates):
            preview = self._truncate(c.get("content", ""), 260)
            source = (c.get("metadata") or {}).get("source", "")
            if source:
                lines.append(f"{i}. [source={source}] {preview}")
            else:
                lines.append(f"{i}. {preview}")
        return f"""
你是一个检索重排序器。请根据用户问题对候选文档片段按相关性从高到低排序。
用户问题：
{query}
候选列表：
{chr(10).join(lines)}
最终要求（非常重要）：
- 只输出一行 JSON
- 严禁输出 markdown（包括 ```json）
- 严禁输出任何解释文字
- 输出必须以 '{"开头，以 "}' 结尾
- JSON 必须只包含一个键：order
{{"order":[0,2,1,3]}}
规则：
- order 必须包含所有候选索引 0..N-1，且不重复
- order 长度必须等于候选数量 N
""".strip()

    def _safe_parse_order(self, raw: str, n: int) -> Optional[List[int]]:
        # 直接 json.loads
        try:
            data = json.loads(raw)
            order = data.get("order")
            if isinstance(order, list) and len(order) == n and sorted(order) == list(range(n)):
                return order
        except Exception:
            pass

        # 从文本中抠出第一个 {...}
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
            order = data.get("order")
            if isinstance(order, list) and len(order) == n and sorted(order) == list(range(n)):
                return order
        except Exception:
            return None
        return None

    async def search(
        self,
        query: str,
        top_k: int = 3,
        metadata_filters: Optional[Dict] = None,
        rerank: Optional[bool] = None,
    ) -> List[Dict]:
        try:
            normalized_filters = normalize_metadata_filters(metadata_filters)
            candidate_limit = max(self.dense_top_k, top_k)
            if normalized_filters:
                candidate_limit = max(candidate_limit * 5, top_k * 10, 30)

            query_vector = await self.embedding.embed_text(query)
            results = await asyncio.to_thread(self._search_sync, query_vector, candidate_limit)
            docs = []
            for hit in results[0]:
                docs.append(
                    {
                        "content": hit.entity.get("content"),
                        "metadata": self._normalize_metadata(hit.entity.get("metadata")),
                        "score": hit.score,
                    }
                )
            # ===== 新增：混合检索融合 =====
            if self.enable_hybrid:
                with self._bm25_lock:
                    bm25_retriever = self.bm25_retriever
                bm25_docs = (
                    await asyncio.to_thread(bm25_retriever.search, query, candidate_limit)
                    if bm25_retriever is not None
                    else []
                )
                if bm25_docs:
                    docs = self._rrf_merge(docs, bm25_docs, top_k=candidate_limit)

            if not docs:
                logger.info("检索到0个相关文档")
                return []

            if normalized_filters:
                before_filter_count = len(docs)
                docs = [doc for doc in docs if matches_metadata_filters(doc.get("metadata"), normalized_filters)]
                logger.info(
                    f"[VectorStore.search] metadata_filters={normalized_filters} "
                    f"filtered {before_filter_count} -> {len(docs)}"
                )
                if not docs:
                    logger.info("metadata 过滤后无结果")
                    return []

            before = [(d.get("metadata") or {}).get("source", "") for d in docs[:3]]
            logger.info(
                f"[VectorStore.search] 候选数={len(docs)} "
                f"(dense_top_k={self.dense_top_k}, top_k={top_k}, candidate_limit={candidate_limit})"
            )
            if rerank is None:
                rerank = self.enable_rerank
            if rerank:
                docs = await self.rerank_documents(query, docs, top_k=candidate_limit)
            after = [(d.get("metadata") or {}).get("source", "") for d in docs[:3]]
            logger.info(f"[VectorStore.search] top3 before={before} after={after}")
            docs = docs[:top_k]
            logger.info(f"检索到 {len(docs)} 个相关文档")
            logger.info(f"[VectorStore.search] 返回数={len(docs)} / 候选数={candidate_limit} (top_k={top_k})")
            return docs
        except Exception as e:
            logger.error(f"检索文档失败: {str(e)}")
            raise Exception(f"检索文档失败: {str(e)}")

    async def rerank_documents(self, query: str, docs: List[Dict], top_k: Optional[int] = None) -> List[Dict]:
        """对候选文档执行配置的重排；多查询检索会在跨查询融合后只调用一次。"""
        if not docs or not self.enable_rerank or len(docs) <= 1:
            return docs[:top_k] if top_k else docs
        limit = top_k or len(docs)
        if self.reranker is not None:
            docs = self.reranker.rerank(query, docs, top_k=limit)
            logger.info("[VectorStore.search] Rerank 模型重排完成")
            return docs

        if self.reranker_llm is None:
            return docs[:limit]
        try:
            prompt = self._build_rerank_prompt(query, docs)
            messages = [
                SystemMessage(content="你是一个严格输出JSON的重排序器。"),
                HumanMessage(content=prompt),
            ]
            resp = await self.reranker_llm.ainvoke(messages)
            raw = resp.content if hasattr(resp, "content") else str(resp)
            order = self._safe_parse_order(raw, n=len(docs))
            if order is not None:
                docs = [docs[i] for i in order]
                logger.info("[VectorStore.search] rerank 成功，order=%s", order)
            else:
                logger.warning("rerank JSON 解析失败 raw=%r", raw)
        except Exception as e:
            logger.warning("rerank 失败，已回退融合排序: %s", e)
        return docs[:limit]

    def _rrf_merge(self, vector_docs: List[Dict], bm25_docs: List[Dict], top_k: int, k: int = 60) -> List[Dict]:
        """简单的 RRF 融合"""
        scores = {}
        for rank, doc in enumerate(bm25_docs):
            key = document_key(doc)
            scores[key] = scores.get(key, {"doc": doc, "score": 0})
            scores[key]["score"] += 1 / (k + rank + 1)
        for rank, doc in enumerate(vector_docs):
            key = document_key(doc)
            scores[key] = scores.get(key, {"doc": doc, "score": 0})
            scores[key]["score"] += 1 / (k + rank + 1)
        return [v["doc"] for v in sorted(scores.values(), key=lambda x: x["score"], reverse=True)[:top_k]]

    def _delete_by_source_sync(self, source: str) -> int:
        """在线程中执行同步 Milvus 删除和 BM25 派生索引更新。"""
        escaped_source = source.replace("\\", "\\\\").replace("'", "\\'")
        expr = f"metadata['source'] == '{escaped_source}'"
        results = self.milvus.collection.query(expr=expr, output_fields=["id"])
        if not results:
            return 0

        ids = [result["id"] for result in results]
        self.milvus.collection.delete(f"id in {ids}")
        if hasattr(self.milvus.collection, "flush"):
            self.milvus.collection.flush()

        with self._bm25_lock:
            self.all_chunks = [c for c in self.all_chunks if document_source(c) != source]
            if self.enable_hybrid:
                self._rebuild_bm25_sync(self.all_chunks)
        return len(ids)

    async def delete_by_source(self, source: str) -> None:
        """删除指定来源的所有文档，不在事件循环中执行同步 Milvus 操作。"""
        try:
            deleted_count = await asyncio.to_thread(self._delete_by_source_sync, source)
            if deleted_count:
                logger.info(f"已删除来源为 {source} 的 {deleted_count} 个文档")
            else:
                logger.info(f"没有找到来源为 {source} 的文档，跳过删除")
        except Exception as e:
            logger.error(f"删除文档失败: {str(e)}")
            raise Exception(f"删除文档失败: {str(e)}")
