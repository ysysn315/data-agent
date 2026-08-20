"""RAG 生成侧消融：同一生成模型（glm-5.3）下，检索质量对最终回答的影响。

    python -m evals.rag.run_generation_ablation [--cases N]

四组（生成模型 glm-5.3 固定；回答约束基本对齐——no-RAG 组用同款"基于上下文/无法确定"
模板但无上下文可注入，检索组走 generate_answer 内部模板，两者非逐字相同，见 REPORTS.md 已知局限）：
  A. dense-only 检索        hybrid=F, rerank=F                （差检索喂给 LLM）
  D. 全开检索（LLM 重排）    hybrid=T, rerank=T(prefer=llm)     （主链路默认）
  E. 全开检索（BGE 重排）    hybrid=T, rerank=T(prefer=bge)     （本地交叉编码器对照）
  N. 无检索（no-RAG）        不做检索，仅凭模型自身知识回答（对照组，考幻觉）

数据集：60 条正式模板（关键词召回 / 严格来源召回 / 禁引来源违规率；逐例存 pred_sources 可离线重算）。
报告落 evals/rag/reports/ablation_generation_<时间戳>.json + _latest.json 副本（不覆盖历史）。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from statistics import mean
from typing import Literal

from loguru import logger

from app.core.settings import get_settings
from evals.rag.common import build_rag, load_json, save_json
from evals.rag.metrics import keyword_recall, source_recall_strict

DATASET_PATH = "evals/rag/datasets/rag_generation_cases_formal_template.json"
REPORT_STEM = "evals/rag/reports/ablation_generation"  # 带戳版 + latest 两份，不覆盖历史

RerankPrefer = Literal["auto", "bge", "llm"]


def _resolve_expected_sources(case: dict) -> tuple[list, list]:
    """按字段存在性解析 (expected_all, expected_any)，供 eval_group 与测试共用。

    expected_sources_any 键存在即以此为准（空列表=无 any 条件）；
    仅该键缺失时兼容旧字段 expected_sources——不能用 or 链，否则空 any
    会回退成 all 的复制，多源题同时按 all 与 any 双重计分。
    """
    expected_all = case.get("expected_sources_all") or []
    if "expected_sources_any" in case:
        expected_any = case.get("expected_sources_any") or []
    else:
        expected_any = case.get("expected_sources") or []
    return expected_all, expected_any


# 生成模型显式钉死（不依赖 .env 隐式约定），no-RAG 组与 RAG 组共用同一模型
GENERATION_MODEL = "glm-5.3"

# 与 RAG 组（rag_service.generate_answer 内部 prompt）对齐的回答要求——
# no-RAG 对照组的差异只在"没有检索上下文"，不在 prompt 风格（评审指出简要/完整措辞会混淆 keyword_recall）
NO_CONTEXT_PROMPT = (
    "你是运维知识助手。请基于上下文回答问题；如果上下文没有提供答案，"
    "明确说明无法从已有资料确定，不要编造。\n\n上下文：无（未检索）\n\n问题：{question}"
)


async def eval_group(cases, mode: str, prefer: RerankPrefer = "llm") -> dict:
    """mode: dense / full / none（none=无检索对照）；prefer 显式控制重排类型不静默回退。"""
    if mode != "none":
        rag_service, _ = await build_rag(
            enable_hybrid=(mode == "full"),
            enable_rerank=(mode == "full"),
            dense_top_k=10,
            rerank_prefer=prefer,
        )
        if mode == "full":
            restored = await rag_service.vector_store.restore_bm25_index()
            logger.info(f"BM25 恢复 {restored} 篇")
    else:
        rag_service, _ = None, None

    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate

    from app.core.llm import LLMFactory

    llm = LLMFactory.create_llm(model=GENERATION_MODEL, temperature=0.0, streaming=False)
    bare_chain = ChatPromptTemplate.from_template(NO_CONTEXT_PROMPT) | llm | StrOutputParser()

    kw_list, src_hit_list, forbidden_violation_list = [], [], []
    per_case = []
    for c in cases:
        question = c["question"]
        expected_keywords = c.get("expected_keywords") or []
        expected_all, expected_any = _resolve_expected_sources(c)
        forbidden_sources = c.get("forbidden_sources") or []

        if mode == "none":
            answer = await bare_chain.ainvoke({"question": question})
            sources = []
        else:
            out = await rag_service.generate_answer(question)
            answer = out.get("answer", "")
            sources = out.get("sources", [])

        kw = keyword_recall(answer, expected_keywords)
        # 严格口径（metrics.source_recall_strict）：all=必须全部命中、any=任一即得分——
        # 原实现把 all 压平成 any，多源题（9 例）被放宽、all/any 并存（6 例）语义丢失
        sh = source_recall_strict(sources, expected_sources_all=expected_all, expected_sources_any=expected_any)
        fv = 1.0 if any(f in sources for f in forbidden_sources) else 0.0

        kw_list.append(kw)
        src_hit_list.append(sh)
        forbidden_violation_list.append(fv)
        per_case.append(
            {
                "id": c["id"],
                "question_type": c.get("question_type"),
                "keyword_recall": round(kw, 4),
                "source_recall_strict": round(sh, 4),
                "forbidden_violation": fv,
                "pred_sources": sources,  # 离线审计用：可重算任意来源指标
                "answer_preview": answer[:200],
            }
        )

    return {
        "summary": {
            "retrieval_rerank_type": "none" if mode != "full" else ("bge" if prefer == "bge" else "llm"),
            "keyword_recall": round(mean(kw_list), 4),
            # 严格来源召回（all 全中 + any 任一，数据集声明口径）；原 source_hit 为宽松 any 版本
            "source_recall_strict": round(mean(src_hit_list), 4),
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

    modes = [
        ("A_dense_retrieval", "dense", "llm"),
        ("D_full_llm_rerank", "full", "llm"),
        ("E_full_bge_rerank", "full", "bge"),
        ("N_no_rag", "none", "llm"),
    ]
    results = {}
    for name, mode, prefer in modes:
        logger.info(f"=== {name} (mode={mode}, prefer={prefer}) ===")
        results[name] = await eval_group(cases, mode, prefer=prefer)
        logger.info(str(results[name]["summary"]))

    report = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "num_cases": len(cases),
            "generation_model": GENERATION_MODEL,
            "embedding": f"{get_settings().embedding_provider}/{get_settings().embedding_model}",
        },
        "groups": results,
    }
    stamped = f"{REPORT_STEM}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    save_json(stamped, report)
    save_json(f"{REPORT_STEM}_latest.json", report)  # latest 是戳版副本，历史在带戳文件
    logger.info(f"saved: {stamped} (+latest 副本)")


if __name__ == "__main__":
    asyncio.run(main())
