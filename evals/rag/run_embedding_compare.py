import asyncio
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Tuple

from langchain_community.chat_models import ChatTongyi
from loguru import logger

from app.clients.milvus_client import MilvusClient
from app.core.settings import Settings, get_settings
from app.rag.bm25 import BM25Retriever
from app.rag.chunking import DocumentChunker, get_strategy_by_filename
from app.rag.embeddings import EmbeddingService
from app.rag.load import DocumentLoader
from app.rag.metadata_filters import build_base_metadata, merge_chunk_metadata
from app.rag.query_rewriter import QueryRewriter
from app.rag.reranker import BGEReranker
from app.rag.vector_store import VectorStore
from app.services.rag_service import RAGService
from evals.rag.common import load_json, save_json
from evals.rag.kb_tools import (
    batch_index_documents,
    clear_current_knowledge_base,
    collect_test_documents,
)
from evals.rag.metrics import (
    fact_recall_strict,
    forbidden_source_score,
    hallucination_score,
    hit_at_k,
    keyword_recall,
    mean_average_precision,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    source_hit,
    source_precision_strict,
    source_recall_strict,
    strict_generation_score,
)


RETRIEVAL_DATASET_PATH = "evals/rag/datasets/rag_retrieval_cases.json"
GENERATION_DATASET_PATH = "evals/rag/datasets/rag_generation_cases_formal_template.json"
REPORT_PATH = "evals/rag/reports/embedding_compare_latest.json"

FIXED_MAIN_MODEL = "qwen3-max"
FIXED_REWRITE_MODEL = "qwen-turbo"
FIXED_RERANK_MODEL = "qwen-turbo"
FIXED_ENABLE_HYBRID = True
FIXED_ENABLE_RERANK = True
FIXED_DENSE_TOP_K = 10


def build_experiments(base_settings: Settings) -> List[Dict]:
    """独立 collection 跑实验，默认 collection 仍保留原始 embedding。"""
    return [
        {
            "name": "text-embedding-v4",
            "embedding_provider": "dashscope",
            "embedding_model": "text-embedding-v4",
            "collection": base_settings.milvus_collection,
            "is_default_collection": True,
        },
        {
            "name": "bge-large-zh",
            "embedding_provider": "bge",
            "embedding_model": "bge-large-zh",
            "collection": f"{base_settings.milvus_collection}_bge_large_zh",
            "is_default_collection": False,
        },
    ]


def clone_settings(base_settings: Settings, **overrides) -> Settings:
    payload = base_settings.model_dump()
    payload.update(overrides)
    return Settings(**payload)


def build_chunk_cache(settings: Settings, doc_paths: Iterable[Path]) -> List[Dict]:
    """给 hybrid 检索补一份本地 BM25 索引，避免评测阶段只有向量检索。"""
    chunks: List[Dict] = []

    for path in doc_paths:
        records = DocumentLoader.load_records(str(path))
        if not records:
            continue

        chunker = DocumentChunker(
            strategy=get_strategy_by_filename(path.name),
            max_size=settings.doc_chunk_max_size,
            overlap=settings.doc_chunk_overlap,
        )
        base_metadata = build_base_metadata(path.name, title=path.stem)

        for record in records:
            text = str(record.get("content", "") or "")
            if not text.strip():
                continue

            extra_metadata = record.get("metadata") or {}
            for chunk in chunker.chunk_text(text, path.name):
                merged_metadata = merge_chunk_metadata(
                    base_metadata=base_metadata,
                    extra_metadata=extra_metadata,
                    chunk_metadata=chunk.get("metadata") or {},
                )
                chunks.append(
                    {
                        "content": chunk["content"],
                        "metadata": merged_metadata,
                    }
                )

    return chunks


def build_eval_rerankers(settings: Settings):
    reranker = None
    try:
        reranker = BGEReranker("BAAI/bge-reranker-base")
        logger.info("[embedding-compare] Using BGE reranker.")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[embedding-compare] BGE reranker unavailable, fallback to LLM rerank: {e}"
        )

    reranker_llm = ChatTongyi(
        dashscope_api_key=settings.dashscope_api_key,
        model_name=FIXED_RERANK_MODEL,
        streaming=False,
        temperature=0.0,
    )
    return reranker, reranker_llm


async def build_eval_stack(
    settings: Settings,
    chunk_cache: List[Dict],
) -> Tuple[RAGService, VectorStore, MilvusClient]:
    milvus_client = MilvusClient(settings)
    await milvus_client.connect()
    await milvus_client.ensure_collection()

    embedding_service = EmbeddingService(settings)
    reranker, reranker_llm = build_eval_rerankers(settings)

    vector_store = VectorStore(
        milvus_client=milvus_client,
        embedding_service=embedding_service,
        reranker_llm=reranker_llm,
        reranker=reranker,
        dense_top_k=FIXED_DENSE_TOP_K,
        enable_rerank=FIXED_ENABLE_RERANK,
        enable_hybrid=FIXED_ENABLE_HYBRID,
    )

    # 评测阶段重建一份 BM25，保证 hybrid 路径和插入阶段一致。
    if FIXED_ENABLE_HYBRID and chunk_cache:
        vector_store.all_chunks = list(chunk_cache)
        vector_store.bm25_retriever = BM25Retriever()
        vector_store.bm25_retriever.index(vector_store.all_chunks)

    generation_llm = ChatTongyi(
        dashscope_api_key=settings.dashscope_api_key,
        model_name=FIXED_MAIN_MODEL,
        streaming=False,
        temperature=0.0,
    )
    rewrite_llm = ChatTongyi(
        dashscope_api_key=settings.dashscope_api_key,
        model_name=FIXED_REWRITE_MODEL,
        streaming=False,
        temperature=0.0,
    )

    rag_service = RAGService(vector_store, generation_llm)
    rag_service.query_rewriter = QueryRewriter(rewrite_llm)
    return rag_service, vector_store, milvus_client


async def run_retrieval_eval(vector_store: VectorStore, cases: List[Dict]) -> Dict:
    per_case = []
    h1_list, h3_list, r3_list, mrr_list = [], [], [], []
    p3_list, ndcg3_list = [], []
    all_pred, all_gold = [], []

    for case in cases:
        query = case["query"]
        gold = set(case["gold_sources"])

        docs = await vector_store.search(query, top_k=3)
        pred = []
        for doc in docs:
            source = (doc.get("metadata") or {}).get("source", "")
            if source:
                pred.append(source)

        h1 = hit_at_k(pred, gold, 1)
        h3 = hit_at_k(pred, gold, 3)
        r3 = recall_at_k(pred, gold, 3)
        mrr_value = mrr(pred, gold)
        p3 = precision_at_k(pred, gold, 3)
        ndcg3 = ndcg_at_k(pred, gold, 3)

        h1_list.append(h1)
        h3_list.append(h3)
        r3_list.append(r3)
        mrr_list.append(mrr_value)
        p3_list.append(p3)
        ndcg3_list.append(ndcg3)
        all_pred.append(pred)
        all_gold.append(gold)

        per_case.append(
            {
                "id": case["id"],
                "query": query,
                "gold_sources": sorted(gold),
                "pred_sources": pred,
                "hit@1": h1,
                "hit@3": h3,
                "recall@3": r3,
                "mrr": mrr_value,
                "precision@3": p3,
                "ndcg@3": ndcg3,
            }
        )

    return {
        "summary": {
            "num_cases": len(cases),
            "hit@1": round(mean(h1_list), 4) if h1_list else 0.0,
            "hit@3": round(mean(h3_list), 4) if h3_list else 0.0,
            "recall@3": round(mean(r3_list), 4) if r3_list else 0.0,
            "mrr": round(mean(mrr_list), 4) if mrr_list else 0.0,
            "precision@3": round(mean(p3_list), 4) if p3_list else 0.0,
            "ndcg@3": round(mean(ndcg3_list), 4) if ndcg3_list else 0.0,
            "map": round(mean_average_precision(all_pred, all_gold), 4)
            if all_pred
            else 0.0,
        },
        "cases": per_case,
    }


async def run_generation_eval(rag_service: RAGService, cases: List[Dict]) -> Dict:
    per_case = []
    keyword_scores = []
    legacy_source_scores = []
    fact_scores = []
    source_recall_scores = []
    source_precision_scores = []
    hallucination_scores = []
    forbidden_source_scores = []
    strict_scores = []
    errors = 0

    for case in cases:
        question = case["question"]
        expected_keywords = case.get("expected_keywords", [])
        expected_sources = case.get("expected_sources", [])
        expected_sources_all = case.get("expected_sources_all", expected_sources)
        expected_sources_any = case.get("expected_sources_any", [])
        expected_facts = case.get("expected_facts", [])
        forbidden_claims = case.get("forbidden_claims", [])
        forbidden_sources = case.get("forbidden_sources", [])

        try:
            output = await rag_service.generate_answer(question)
            answer = output.get("answer", "")
            sources = output.get("sources", [])
            status = "success"
            error_message = ""
        except Exception as e:  # noqa: BLE001
            answer = ""
            sources = []
            status = "error"
            error_message = str(e)
            errors += 1

        keyword_score = keyword_recall(answer, expected_keywords)
        legacy_source_score = source_hit(
            sources,
            expected_sources_all + expected_sources_any,
        )
        fact_score = fact_recall_strict(answer, expected_facts)
        source_recall_score = source_recall_strict(
            sources,
            expected_sources_all=expected_sources_all,
            expected_sources_any=expected_sources_any,
        )
        source_precision_score = source_precision_strict(
            sources,
            expected_sources_all=expected_sources_all,
            expected_sources_any=expected_sources_any,
        )
        hallucination_score_value = hallucination_score(answer, forbidden_claims)
        forbidden_source_score_value = forbidden_source_score(
            sources,
            forbidden_sources=forbidden_sources,
        )
        strict_score = strict_generation_score(
            fact_score=fact_score,
            source_recall_score=source_recall_score,
            source_precision_score=source_precision_score,
            hallucination_score_value=hallucination_score_value,
            forbidden_source_score_value=forbidden_source_score_value,
        )

        keyword_scores.append(keyword_score)
        legacy_source_scores.append(legacy_source_score)
        fact_scores.append(fact_score)
        source_recall_scores.append(source_recall_score)
        source_precision_scores.append(source_precision_score)
        hallucination_scores.append(hallucination_score_value)
        forbidden_source_scores.append(forbidden_source_score_value)
        strict_scores.append(strict_score)

        per_case.append(
            {
                "id": case["id"],
                "question": question,
                "status": status,
                "question_type": case.get("question_type", "unknown"),
                "difficulty": case.get("difficulty", "unknown"),
                "pred_sources": sources,
                "keyword_recall": round(keyword_score, 4),
                "source_hit": round(legacy_source_score, 4),
                "fact_recall": round(fact_score, 4),
                "source_recall_strict": round(source_recall_score, 4),
                "source_precision_strict": round(source_precision_score, 4),
                "hallucination_score": round(hallucination_score_value, 4),
                "forbidden_source_score": round(forbidden_source_score_value, 4),
                "strict_score": round(strict_score, 4),
                "answer_preview": answer[:300],
                "error": error_message,
            }
        )

    return {
        "summary": {
            "num_cases": len(cases),
            "keyword_recall": round(mean(keyword_scores), 4) if keyword_scores else 0.0,
            "source_hit": round(mean(legacy_source_scores), 4)
            if legacy_source_scores
            else 0.0,
            "fact_recall": round(mean(fact_scores), 4) if fact_scores else 0.0,
            "source_recall_strict": round(mean(source_recall_scores), 4)
            if source_recall_scores
            else 0.0,
            "source_precision_strict": round(mean(source_precision_scores), 4)
            if source_precision_scores
            else 0.0,
            "hallucination_score": round(mean(hallucination_scores), 4)
            if hallucination_scores
            else 0.0,
            "forbidden_source_score": round(mean(forbidden_source_scores), 4)
            if forbidden_source_scores
            else 0.0,
            "strict_score": round(mean(strict_scores), 4) if strict_scores else 0.0,
            "error_rate": round(errors / len(cases), 4) if cases else 0.0,
        },
        "cases": per_case,
    }


async def run_one_experiment(
    base_settings: Settings,
    experiment: Dict,
    doc_paths: List[Path],
    retrieval_cases: List[Dict],
    generation_cases: List[Dict],
    chunk_cache: List[Dict],
) -> Dict:
    settings = clone_settings(
        base_settings,
        chat_model=FIXED_MAIN_MODEL,
        embedding_provider=experiment["embedding_provider"],
        embedding_model=experiment["embedding_model"],
        milvus_collection=experiment["collection"],
    )

    clear_result = await clear_current_knowledge_base(settings)
    index_result = await batch_index_documents(settings, doc_paths)

    rag_service = None
    vector_store = None
    milvus_client = None
    try:
        rag_service, vector_store, milvus_client = await build_eval_stack(
            settings=settings,
            chunk_cache=chunk_cache,
        )
        retrieval_report = await run_retrieval_eval(vector_store, retrieval_cases)
        generation_report = await run_generation_eval(rag_service, generation_cases)
    finally:
        if milvus_client is not None:
            await milvus_client.close()

    return {
        "experiment": experiment,
        "fixed_config": {
            "main_model": FIXED_MAIN_MODEL,
            "rewrite_model": FIXED_REWRITE_MODEL,
            "rerank_model": FIXED_RERANK_MODEL,
            "enable_hybrid": FIXED_ENABLE_HYBRID,
            "enable_rerank": FIXED_ENABLE_RERANK,
            "dense_top_k": FIXED_DENSE_TOP_K,
        },
        "clear_result": clear_result,
        "index_result": index_result,
        "chunk_cache_size": len(chunk_cache),
        "retrieval": retrieval_report,
        "generation": generation_report,
    }


async def main():
    base_settings = get_settings()
    doc_paths = collect_test_documents()
    retrieval_cases = load_json(RETRIEVAL_DATASET_PATH)
    generation_cases = load_json(GENERATION_DATASET_PATH)
    experiments = build_experiments(base_settings)

    results = []
    for experiment in experiments:
        settings = clone_settings(
            base_settings,
            embedding_provider=experiment["embedding_provider"],
            embedding_model=experiment["embedding_model"],
            milvus_collection=experiment["collection"],
        )
        chunk_cache = build_chunk_cache(settings, doc_paths)
        result = await run_one_experiment(
            base_settings=base_settings,
            experiment=experiment,
            doc_paths=doc_paths,
            retrieval_cases=retrieval_cases,
            generation_cases=generation_cases,
            chunk_cache=chunk_cache,
        )
        results.append(result)
        print(
            experiment["name"],
            {
                "retrieval": result["retrieval"]["summary"],
                "generation": result["generation"]["summary"],
            },
        )

    leaderboard = sorted(
        results,
        key=lambda item: (
            -item["generation"]["summary"]["strict_score"],
            -item["retrieval"]["summary"]["recall@3"],
            -item["retrieval"]["summary"]["mrr"],
            item["generation"]["summary"]["error_rate"],
        ),
    )

    report = {
        "retrieval_dataset_path": RETRIEVAL_DATASET_PATH,
        "generation_dataset_path": GENERATION_DATASET_PATH,
        "doc_dirs": ["aiops-docs", "aiops-docs-noise"],
        "leaderboard": leaderboard,
        "best": leaderboard[0] if leaderboard else None,
    }
    save_json(REPORT_PATH, report)
    print("saved:", REPORT_PATH)


if __name__ == "__main__":
    asyncio.run(main())
