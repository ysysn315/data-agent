# 向量存储模块
# TODO: 任务 12.2 - 实现 VectorStore 类
# 向量存储模块
# TODO: 任务 12.2 - 实现 VectorStore 类

from typing import List, Dict
from app.clients.milvus_client import MilvusClient
from app.rag.embeddings import EmbeddingService
from loguru import logger
import json
import re
from typing import Optional
from langchain_core.messages import SystemMessage, HumanMessage
from app.rag.metadata_filters import (
    matches_metadata_filters,
    normalize_metadata_filters,
)


class VectorStore:
    def __init__(self, milvus_client: MilvusClient, embedding_service: EmbeddingService,
                 reranker_llm: Optional[object] = None,reranker: Optional[object] = None, dense_top_k: int = 10, enable_rerank: bool = True,
                 enable_hybrid: bool = True):
        self.milvus = milvus_client
        self.embedding = embedding_service
        self.reranker_llm = reranker_llm
        self.reranker = reranker 
        self.dense_top_k = dense_top_k
        self.enable_rerank = enable_rerank
        self.enable_hybrid = enable_hybrid
        self.bm25_retriever = None  # 新增
        self.all_chunks = []  # 新增：存储所有文档用于 BM25
        logger.info("向量存储初始化完成")

    async def insert(self, chunks: List[Dict]) -> None:
        try:
            if not chunks:
                logger.warning("没有文档需要插入")
                return
            texts = [chunk["content"] for chunk in chunks]
            vectors = await self.embedding.embed_texts(texts)
            data = [
                vectors, texts, [chunk["metadata"] for chunk in chunks]
            ]
            self.milvus.collection.insert(data)
            self.milvus.collection.flush()
            logger.info(f"成功插入 {len(chunks)} 个文档块")
            self.all_chunks.extend(chunks)
            if self.enable_hybrid:
                from app.rag.bm25 import BM25Retriever
                self.bm25_retriever = BM25Retriever()
                self.bm25_retriever.index(self.all_chunks)

        except Exception as e:
            logger.error(f"插入文档失败: {str(e)}")
            raise Exception(f"插入文档失败: {str(e)}")

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
- 输出必须以 '{'开头，以 '}' 结尾
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
    ) -> List[Dict]:
        try:
            normalized_filters = normalize_metadata_filters(metadata_filters)
            candidate_limit = max(self.dense_top_k, top_k)
            if normalized_filters:
                candidate_limit = max(candidate_limit * 5, top_k * 10, 30)

            query_vector = await self.embedding.embed_text(query)
            results = self.milvus.collection.search(
                data=[query_vector],
                anns_field="vector",
                param={"metric_type": "IP", "params": {"nprobe": 10}},
                limit=candidate_limit,
                output_fields=["content", "metadata"]
            )
            docs = []
            for hit in results[0]:
                docs.append(
                    {
                        "content": hit.entity.get("content"),
                        "metadata": hit.entity.get("metadata"),
                        "score": hit.score
                    }
                )
            # ===== 新增：混合检索融合 =====
            if self.enable_hybrid and self.bm25_retriever is not None:
                bm25_docs = self.bm25_retriever.search(query, top_k=candidate_limit)
                docs = self._rrf_merge(docs, bm25_docs, top_k=candidate_limit)

            if not docs:
                logger.info("检索到0个相关文档")
                return []

            if normalized_filters:
                before_filter_count = len(docs)
                docs = [
                    doc for doc in docs
                    if matches_metadata_filters(doc.get("metadata"), normalized_filters)
                ]
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
            if self.enable_rerank and len(docs) > 1:
                if self.reranker is not None:
                    docs=self.reranker.rerank(query,docs,top_k=candidate_limit)
                    logger.info("[VectorStore.search] Rerank 模型重排完成")
                      
                elif self.reranker_llm is not None:

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
                            logger.info(f"[VectorStore.search] rerank 成功，order={order}")
                        else:
                            logger.warning(f"rerank JSON 解析失败 raw={raw!r}")
                    except Exception as e:
                        logger.warning(f"rerank 失败，已回退向量排序: {e}")
            after = [(d.get("metadata") or {}).get("source", "") for d in docs[:3]]
            logger.info(f"[VectorStore.search] top3 before={before} after={after}")
            docs = docs[:top_k]
            logger.info(f"检索到 {len(docs)} 个相关文档")
            logger.info(
                f"[VectorStore.search] 返回数={len(docs)} / 候选数={candidate_limit} (top_k={top_k})"
            )
            return docs
        except Exception as e:
            logger.error(f"检索文档失败: {str(e)}")
            raise Exception(f"检索文档失败: {str(e)}")

    def _rrf_merge(self, vector_docs: List[Dict], bm25_docs: List[Dict], top_k: int, k: int = 60) -> List[Dict]:
        """简单的 RRF 融合"""
        scores = {}
        for rank, doc in enumerate(bm25_docs):
            key = doc["content"][:100]
            scores[key] = scores.get(key, {"doc": doc, "score": 0})
            scores[key]["score"] += 1 / (k + rank + 1)
        for rank, doc in enumerate(vector_docs):
            key = doc["content"][:100]
            scores[key] = scores.get(key, {"doc": doc, "score": 0})
            scores[key]["score"] += 1 / (k + rank + 1)
        return [v["doc"] for v in sorted(scores.values(), key=lambda x: x["score"], reverse=True)[:top_k]]

    async def delete_by_source(self, source: str) -> None:
        """
        删除指定来源的所有文档

        Args:
            source: 文件名
        """
        try:
            # 先查询出所有匹配的文档 ID
            expr = f"metadata['source'] == '{source}'"

            # 查询匹配的文档
            results = self.milvus.collection.query(
                expr=expr,
                output_fields=["id"]
            )

            if not results:
                logger.info(f"没有找到来源为 {source} 的文档，跳过删除")
                return

            # 提取所有 ID
            ids = [result["id"] for result in results]

            # 按 ID 删除
            delete_expr = f"id in {ids}"
            self.milvus.collection.delete(delete_expr)

            # 新增：同步删除 all_chunks 并重建 BM25 索引
            self.all_chunks = [c for c in self.all_chunks if c.get("metadata", {}).get("source") != source]
            if self.enable_hybrid and self.bm25_retriever is not None:
                self.bm25_retriever.index(self.all_chunks)

            logger.info(f"已删除来源为 {source} 的 {len(ids)} 个文档")

        except Exception as e:
            logger.error(f"删除文档失败: {str(e)}")
            raise Exception(f"删除文档失败: {str(e)}")
