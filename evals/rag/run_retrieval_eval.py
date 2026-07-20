import asyncio
from statistics import mean

from evals.rag.common import load_json, save_json, build_rag
from evals.rag.metrics import hit_at_k, recall_at_k, mrr, precision_at_k, ndcg_at_k, mean_average_precision

DATASET_PATH = "evals/rag/datasets/rag_retrieval_cases.json"
REPORT_PATH = "evals/rag/reports/retrieval_latest.json"


async def main():
    cases = load_json(DATASET_PATH)
    rag_service, vector_store = await build_rag(
        enable_hybrid=True,
        enable_rerank=True,
        dense_top_k=10,
    )

    per_case = []
    h1_list, h3_list, r3_list, mrr_list = [], [], [], []
    p3_list, ndcg3_list = [], []
    all_pred, all_gold = [], []
    for c in cases:
        query = c["query"]
        gold = set(c["gold_sources"])

        docs = await vector_store.search(query, top_k=3)
        pred = []
        for d in docs:
            src = (d.get("metadata") or {}).get("source", "")
            if src:
                pred.append(src)

        h1 = hit_at_k(pred, gold, 1)
        h3 = hit_at_k(pred, gold, 3)
        r3 = recall_at_k(pred, gold, 3)
        m = mrr(pred, gold)

        h1_list.append(h1)
        h3_list.append(h3)
        r3_list.append(r3)
        mrr_list.append(m)

        p3 = precision_at_k(pred, gold, 3)
        ndcg3 = ndcg_at_k(pred, gold, 3)

        p3_list.append(p3)
        ndcg3_list.append(ndcg3)
        all_pred.append(pred)
        all_gold.append(gold)

        per_case.append(
            {
                "id": c["id"],
                "query": query,
                "gold_sources": sorted(list(gold)),
                "pred_sources": pred,
                "hit@1": h1,
                "hit@3": h3,
                "recall@3": r3,
                "mrr": m,
                "precision@3": p3,
                "ndcg@3": ndcg3
            }
        )

    report = {
        "summary": {
            "num_cases": len(cases),
            "hit@1": round(mean(h1_list), 4),
            "hit@3": round(mean(h3_list), 4),
            "recall@3": round(mean(r3_list), 4),
            "mrr": round(mean(mrr_list), 4),
            "precision@3": round(mean(p3_list), 4),
            "ndcg@3": round(mean(ndcg3_list), 4),
            "map": round(mean_average_precision(all_pred, all_gold), 4),
        },
        "cases": per_case,
    }

    save_json(REPORT_PATH, report)
    print("saved:", REPORT_PATH)
    print(report["summary"])


if __name__ == "__main__":
    asyncio.run(main())
