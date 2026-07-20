import json
from pathlib import Path
from typing import Dict, Any

from langchain_community.chat_models import ChatTongyi
from loguru import logger

from app.core.settings import get_settings
from app.clients.milvus_client import MilvusClient
from app.rag.embeddings import EmbeddingService
from app.rag.query_rewriter import QueryRewriter
from app.rag.reranker import BGEReranker
from app.rag.vector_store import VectorStore
from app.services.rag_service import RAGService


def load_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: str, data: Dict[str, Any]):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_eval_rerankers(settings, reranker_model: str = "qwen-turbo"):
    reranker = None
    try:
        reranker = BGEReranker("BAAI/bge-reranker-base")
        logger.info("[evals] Using BGE reranker for retrieval eval.")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[evals] BGE reranker unavailable, falling back to LLM rerank: {e}")

    reranker_llm = ChatTongyi(
        dashscope_api_key=settings.dashscope_api_key,
        model_name=reranker_model,
        streaming=False,
        temperature=0.0,
    )
    return reranker, reranker_llm


async def build_rag(enable_hybrid: bool, enable_rerank: bool, dense_top_k: int = 10):
    settings = get_settings()
    milvus_client = MilvusClient(settings)
    await milvus_client.connect()
    await milvus_client.ensure_collection()

    embedding_service = EmbeddingService(settings)
    reranker, reranker_llm = _build_eval_rerankers(settings, reranker_model="qwen-turbo")

    vector_store = VectorStore(
        milvus_client=milvus_client,
        embedding_service=embedding_service,
        reranker_llm=reranker_llm,
        reranker=reranker,
        dense_top_k=dense_top_k,
        enable_rerank=enable_rerank,
        enable_hybrid=enable_hybrid,
    )

    llm = ChatTongyi(
        dashscope_api_key=settings.dashscope_api_key,
        model_name=settings.chat_model,
        streaming=False,
        temperature=0.0,
    )

    rag_service = RAGService(vector_store, llm)
    return rag_service, vector_store


async def build_rag_for_main_model_eval(
    generation_model: str,
    rewrite_model: str = "qwen-turbo",
    reranker_model: str = "qwen-turbo",
    enable_hybrid: bool = True,
    enable_rerank: bool = True,
    dense_top_k: int = 10,
):
    """
    Eval-only builder that keeps retrieval-side models fixed while swapping
    the main answer-generation model.
    """

    settings = get_settings()
    milvus_client = MilvusClient(settings)
    await milvus_client.connect()
    await milvus_client.ensure_collection()

    embedding_service = EmbeddingService(settings)
    reranker, reranker_llm = _build_eval_rerankers(settings, reranker_model=reranker_model)

    vector_store = VectorStore(
        milvus_client=milvus_client,
        embedding_service=embedding_service,
        reranker_llm=reranker_llm,
        reranker=reranker,
        dense_top_k=dense_top_k,
        enable_rerank=enable_rerank,
        enable_hybrid=enable_hybrid,
    )

    generation_llm = ChatTongyi(
        dashscope_api_key=settings.dashscope_api_key,
        model_name=generation_model,
        streaming=False,
        temperature=0.0,
    )

    rewrite_llm = ChatTongyi(
        dashscope_api_key=settings.dashscope_api_key,
        model_name=rewrite_model,
        streaming=False,
        temperature=0.0,
    )

    rag_service = RAGService(vector_store, generation_llm)
    # Override the default query rewriter so generation-model experiments
    # do not accidentally change the rewrite model at the same time.
    rag_service.query_rewriter = QueryRewriter(rewrite_llm)
    return rag_service, vector_store
