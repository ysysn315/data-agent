"""主 Chat RAG 接线、BM25 恢复和文档列表接口测试。"""

from __future__ import annotations

import httpx
import pytest

from app.agents.tools.internal_docs_tool import create_docs_tool
from app.core.dependencies import get_vector_store
from app.main import app
from app.rag.context import current_sources, use_metadata_filters
from app.rag.vector_store import VectorStore
from app.services.chat_service import ChatService
from app.services.rag_service import RAGService


class _Iterator:
    def __init__(self, batches):
        self._batches = iter(batches)
        self.closed = False

    def next(self):
        return next(self._batches, [])

    def close(self):
        self.closed = True


class _Collection:
    def __init__(self, rows, dense_hits=None):
        self.rows = rows
        self.dense_hits = dense_hits or []
        self.iterator = None

    def query_iterator(self, **_kwargs):
        self.iterator = _Iterator([self.rows, []])
        return self.iterator

    def search(self, **_kwargs):
        return [self.dense_hits]


class _Milvus:
    def __init__(self, collection):
        self.collection = collection


class _Embedding:
    async def embed_text(self, _text):
        return [1.0]


class _Hit:
    def __init__(self, content, metadata, score=1.0):
        self.entity = {"content": content, "metadata": metadata}
        self.score = score


def _rows():
    return [
        {
            "id": 1,
            "content": "数据库连接超时的处理步骤",
            "metadata": {
                "source": "ops.md",
                "title": "运维手册",
                "doc_type": "markdown",
                "ingested_at": 100,
            },
        },
        {
            "id": 2,
            "content": "先检查连接池和网络",
            "metadata": {
                "source": "ops.md",
                "title": "运维手册",
                "doc_type": "markdown",
                "ingested_at": 100,
            },
        },
        {
            "id": 3,
            "content": "订单表的字段说明",
            "metadata": {
                "source": "sales.xlsx",
                "title": "销售数据",
                "doc_type": "excel",
                "sheet_name": "orders",
                "ingested_at": 200,
            },
        },
        {
            "id": 4,
            "content": "客户表的字段说明",
            "metadata": {
                "source": "sales.xlsx",
                "title": "销售数据",
                "doc_type": "excel",
                "sheet_name": "customers",
                "ingested_at": 200,
            },
        },
    ]


@pytest.mark.asyncio
async def test_restore_bm25_and_list_documents_from_milvus():
    collection = _Collection(
        _rows(),
        dense_hits=[_Hit("数据库连接超时的处理步骤", _rows()[0]["metadata"])],
    )
    store = VectorStore(_Milvus(collection), _Embedding(), enable_rerank=False, restore_batch_size=2)

    assert await store.restore_bm25_index() == 4
    assert store.bm25_retriever is not None
    assert store.bm25_retriever.search("连接超时", top_k=1)[0]["metadata"]["source"] == "ops.md"

    documents = await store.list_documents()
    assert documents == [
        {
            "source": "sales.xlsx",
            "title": "销售数据",
            "doc_type": "excel",
            "chunk_count": 2,
            "ingested_at": 200,
            "sheet_names": ["customers", "orders"],
        },
        {
            "source": "ops.md",
            "title": "运维手册",
            "doc_type": "markdown",
            "chunk_count": 2,
            "ingested_at": 100,
            "sheet_names": [],
        },
    ]
    assert collection.iterator.closed


@pytest.mark.asyncio
async def test_restore_bm25_keeps_tail_when_capped():
    collection = _Collection(_rows())
    store = VectorStore(
        _Milvus(collection),
        _Embedding(),
        enable_rerank=False,
        max_bm25_documents=2,
    )

    assert await store.restore_bm25_index() == 2
    assert [chunk["content"] for chunk in store.all_chunks] == [
        "订单表的字段说明",
        "客户表的字段说明",
    ]


@pytest.mark.asyncio
async def test_hybrid_search_uses_restored_bm25_candidates():
    rows = _rows()
    collection = _Collection(
        rows,
        dense_hits=[_Hit("完全不同的向量结果", {"source": "other.md"})],
    )
    store = VectorStore(_Milvus(collection), _Embedding(), enable_rerank=False)
    await store.restore_bm25_index()

    result = await store.search("数据库连接超时", top_k=2)

    assert result
    assert any(doc["metadata"].get("source") == "ops.md" for doc in result)


class _Rewriter:
    async def process_with_expansions(self, query, **_kwargs):
        return [query, "连接池故障"]


class _RAGStore:
    def __init__(self):
        self.search_calls = []
        self.rerank_calls = []

    async def search(self, query, **kwargs):
        self.search_calls.append((query, kwargs))
        return [{"content": query, "metadata": {"source": f"{query}.md"}}]

    async def rerank_documents(self, query, docs, top_k=None):
        self.rerank_calls.append((query, docs, top_k))
        return list(reversed(docs))[:top_k]


@pytest.mark.asyncio
async def test_multi_query_retrieval_merges_before_single_rerank():
    store = _RAGStore()
    rag = RAGService(store, llm=None, query_rewriter=_Rewriter())

    docs = await rag.retrieve_multi_query("数据库超时", top_k=2, metadata_filters={"source": "ops.md"})

    assert [query for query, _kwargs in store.search_calls] == ["数据库超时", "连接池故障"]
    assert all(kwargs["rerank"] is False for _query, kwargs in store.search_calls)
    assert store.search_calls[0][1]["metadata_filters"] == {"source": "ops.md"}
    assert len(store.rerank_calls) == 1
    assert docs[0]["content"] == "连接池故障"


@pytest.mark.asyncio
async def test_docs_tool_merges_request_filters_and_returns_sources():
    class _Retriever:
        def __init__(self):
            self.filters = None

        async def retrieve_multi_query(self, _query, **kwargs):
            self.filters = kwargs["metadata_filters"]
            return [{"content": "正文", "metadata": {"source": "ops.md", "title": "运维"}}]

    retriever = _Retriever()
    tool = create_docs_tool(retriever)
    with use_metadata_filters({"doc_type": "markdown", "title_contains": "运维"}):
        text = await tool.ainvoke({"query": "连接超时", "source": "ops.md"})
        sources = current_sources()

    assert "ops.md" in text
    assert retriever.filters == {
        "doc_type": "markdown",
        "title_contains": "运维",
        "source": "ops.md",
    }
    assert sources == ["ops.md"]


@pytest.mark.asyncio
async def test_documents_endpoint_reads_shared_vector_store():
    class _Store:
        async def list_documents(self):
            return [{"source": "ops.md", "title": "运维", "doc_type": "markdown", "chunk_count": 1}]

    app.dependency_overrides[get_vector_store] = lambda: _Store()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/documents")
    finally:
        app.dependency_overrides.pop(get_vector_store, None)

    assert response.status_code == 200
    assert response.json() == [
        {
            "source": "ops.md",
            "title": "运维",
            "doc_type": "markdown",
            "chunk_count": 1,
            "ingested_at": None,
            "sheet_names": [],
        }
    ]


@pytest.mark.asyncio
async def test_chat_stream_always_returns_structured_events():
    class _Agent:
        async def chat_stream(self, _question, history=None, summary=""):
            yield {"type": "content", "text": "第一段"}
            yield {"type": "content", "text": "第二段"}

    class _SessionStore:
        def __init__(self):
            self.messages = []

        def get_history(self, _session_id):
            return []

        def get_summary(self, _session_id):
            return ""

        def add_message(self, session_id, role, content):
            self.messages.append((session_id, role, content))

    session_store = _SessionStore()
    service = ChatService(_Agent(), session_store)

    events = [event async for event in service.chat_stream("s1", "问题")]

    assert events == [
        {"type": "content", "data": "第一段"},
        {"type": "content", "data": "第二段"},
        {"type": "sources", "data": []},
    ]
    assert session_store.messages == [("s1", "user", "问题"), ("s1", "assistant", "第一段第二段")]
