import json
from pathlib import Path
from typing import Any, Dict

from loguru import logger

from app.clients.milvus_client import MilvusClient
from app.core.llm import LLMFactory
from app.core.settings import get_settings
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


def _build_eval_rerankers(settings, reranker_model: str | None = None, prefer: str = "auto"):
    """构造重排器。prefer: auto（BGE 优先，失败回退 LLM）/ bge（强制 BGE，失败抛错）/
    llm（强制 LLM——消融实验需要显式控制变量，不能静默回退混组）。"""
    reranker = None
    if prefer in ("auto", "bge"):
        try:
            reranker = BGEReranker("BAAI/bge-reranker-base")
            logger.info("[evals] Using BGE reranker for retrieval eval.")
        except Exception as e:  # noqa: BLE001
            if prefer == "bge":
                raise
            logger.warning(f"[evals] BGE reranker unavailable, falling back to LLM rerank: {e}")

    # reranker_model 为 None 时走当前配置的 LLM（aigc 网关无 qwen 系，硬编码会 400）
    reranker_llm = LLMFactory.create_llm(model=reranker_model, temperature=0.0, streaming=False)
    return reranker, reranker_llm


async def build_rag(
    enable_hybrid: bool,
    enable_rerank: bool,
    dense_top_k: int = 10,
    rerank_prefer: str = "auto",
):
    settings = get_settings()
    milvus_client = MilvusClient(settings)
    await milvus_client.connect()
    await milvus_client.ensure_collection()

    embedding_service = EmbeddingService(settings)
    reranker, reranker_llm = _build_eval_rerankers(settings, reranker_model=None, prefer=rerank_prefer)

    vector_store = VectorStore(
        milvus_client=milvus_client,
        embedding_service=embedding_service,
        reranker_llm=reranker_llm,
        reranker=reranker,
        dense_top_k=dense_top_k,
        enable_rerank=enable_rerank,
        enable_hybrid=enable_hybrid,
    )

    llm = LLMFactory.create_llm(model=settings.llm_model, temperature=0.0, streaming=False)

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

    generation_llm = LLMFactory.create_llm(model=generation_model, temperature=0.0, streaming=False)

    rewrite_llm = LLMFactory.create_llm(model=rewrite_model, temperature=0.0, streaming=False)

    rag_service = RAGService(vector_store, generation_llm)
    # Override the default query rewriter so generation-model experiments
    # do not accidentally change the rewrite model at the same time.
    rag_service.query_rewriter = QueryRewriter(rewrite_llm)
    return rag_service, vector_store
