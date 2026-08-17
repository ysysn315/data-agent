"""RAG 生成侧消融：同一生成模型（glm-5.3）下，检索质量对最终回答的影响。

    python -m evals.rag.run_generation_ablation [--cases N]

三组（生成模型与 prompt 完全一致，只变检索输入）：
  A. dense-only 检索    hybrid=False, rerank=False   （差检索喂给 LLM）
  D. 全开检索           hybrid=True,  rerank=True    （主链路默认）
  N. 无检索（no-RAG）    不做检索，仅凭模型自身知识回答（对照组，考幻觉）

数据集：60 条正式模板（关键词召回 / 来源命中 / 禁引来源违规率）。
报告落 evals/rag/reports/ablation_generation.json。
"""

from __future__ import annotations

import argparse
import asyncio
from statistics import mean

from evals.rag.common import build_rag, load_json, save_json
from evals.rag.metrics import keyword_recall

DATASET_PATH = "evals/rag/datasets/rag_generation_cases_formal_template.json"
REPORT_PATH = "evals/rag/reports/ablation_generation.json"

NO_CONTEXT_PROMPT = "你是运维知识助手。请根据你的已有知识简要回答问题；不确定时明确说明。问题：{question}"


async def eval_group(cases, mode: str) -> dict:
    """mode: dense / full / none（none=无检索对照）。"""
    if mode != "none":
        rag_service, _ = await build_rag(
            enable_hybrid=(mode == "full"),
            enable_rerank=(mode == "full"),
            dense_top_k=10,
        )
        if mode == "full":
            restored = await rag_service.vector_store.restore_bm25_index()
            print(f"  BM25 恢复 {restored} 篇")
    else:
        rag_service, _ = None, None

    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate

    from app.core.llm import LLMFactory

    llm = LLMFactory.create_llm(model="glm-5.3", temperature=0.0, streaming=False)
    bare_chain = ChatPromptTemplate.from_template(NO_CONTEXT_PROMPT) | llm | StrOutputParser()

    kw_list, src_hit_list, forbidden_violation_list = [], [], []
    per_case = []
    for c in cases:
        question = c["question"]
        expected_keywords = c.get("expected_keywords") or []
        expected_sources = (
            c.get("expected_sources_all") or c.get("expected_sources_any") or c.get("expected_sources") or []
        )
        forbidden_sources = c.get("forbidden_sources") or []

        if mode == "none":
            answer = await bare_chain.ainvoke({"question": question})
            sources = []
        else:
            out = await rag_service.generate_answer(question)
            answer = out.get("answer", "")
            sources = out.get("sources", [])

        kw = keyword_recall(answer, expected_keywords)
        sh = 1.0 if (not expected_sources or any(s in sources for s in expected_sources)) else 0.0
        fv = 1.0 if any(f in sources for f in forbidden_sources) else 0.0

        kw_list.append(kw)
        src_hit_list.append(sh)
        forbidden_violation_list.append(fv)
        per_case.append(
            {
                "id": c["id"],
                "question_type": c.get("question_type"),
                "keyword_recall": round(kw, 4),
                "source_hit": sh,
                "forbidden_violation": fv,
                "answer_preview": answer[:200],
            }
        )

    return {
        "summary": {
            "keyword_recall": round(mean(kw_list), 4),
            "source_hit": round(mean(src_hit_list), 4),
            "forbidden_violation_rate": round(mean(forbidden_violation_list), 4),
        },
        "cases": per_case,
    }


async def main():
    parser = argparse.ArgumentParser(description="RAG 生成侧消融（检索质量 → 回答质量）")
    parser.add_argument("--cases", type=int, default=None, help="只跑前 N 条（抽样冒烟）")
    args = parser.parse_args()

    cases = load_json(DATASET_PATH)
    if args.cases:
        cases = cases[: args.cases]

    modes = [("A_dense_retrieval", "dense"), ("D_full_retrieval", "full"), ("N_no_rag", "none")]
    results = {}
    for name, mode in modes:
        print(f"\n=== {name} (mode={mode}) ===")
        results[name] = await eval_group(cases, mode)
        print(results[name]["summary"])

    save_json(REPORT_PATH, {"groups": results, "num_cases": len(cases)})
    print(f"\nsaved: {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
