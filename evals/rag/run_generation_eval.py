import asyncio
from statistics import mean

from evals.rag.common import load_json, save_json, build_rag
from evals.rag.metrics import keyword_recall, source_hit


DATASET_PATH = "evals/rag/datasets/rag_generation_cases.json"
REPORT_PATH = "evals/rag/reports/generation_latest.json"


async def main():
    cases = load_json(DATASET_PATH)
    rag_service, _ = await build_rag(
        enable_hybrid=True,
        enable_rerank=True,
        dense_top_k=10,
    )

    per_case = []
    kw_list, src_list = [], []

    for c in cases:
        question = c["question"]
        expected_keywords = c.get("expected_keywords", [])
        expected_sources = c.get("expected_sources", [])

        out = await rag_service.generate_answer(question)
        answer = out.get("answer", "")
        sources = out.get("sources", [])

        kw = keyword_recall(answer, expected_keywords)
        sh = source_hit(sources, expected_sources)

        kw_list.append(kw)
        src_list.append(sh)

        per_case.append(
            {
                "id": c["id"],
                "question": question,
                "expected_keywords": expected_keywords,
                "expected_sources": expected_sources,
                "pred_sources": sources,
                "keyword_recall": round(kw, 4),
                "source_hit": round(sh, 4),
                "answer_preview": answer[:300],
            }
        )

    report = {
        "summary": {
            "num_cases": len(cases),
            "keyword_recall": round(mean(kw_list), 4),
            "source_hit": round(mean(src_list), 4),
        },
        "cases": per_case,
    }

    save_json(REPORT_PATH, report)
    print("saved:", REPORT_PATH)
    print(report["summary"])


if __name__ == "__main__":
    asyncio.run(main())
