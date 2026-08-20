"""RAG 检索消融实验：验证混合检索 / 重排各自的贡献（参考 my-agent 的对照实验思路）。

    python -m evals.rag.run_ablation [--cases N]

六组配置（同一语料库、同一 embedding、同一评测集；重排类型显式控制不静默回退）：
  A. dense-only          hybrid=F, rerank=F               （纯向量基线）
  B. hybrid              hybrid=T, rerank=F               （+BM25 RRF 融合）
  C. dense+LLM重排       hybrid=F, rerank=T(prefer=llm)
  D. hybrid+LLM重排      hybrid=T, rerank=T(prefer=llm)
  E. dense+BGE重排       hybrid=F, rerank=T(prefer=bge)   （本地交叉编码器）
  F. hybrid+BGE重排      hybrid=T, rerank=T(prefer=bge)

指标：Hit@1 / Hit@3 / Recall@3 / MRR / Precision@3 / NDCG@3 / MAP。
报告落 evals/rag/reports/ablation_retrieval.json，含逐组 summary 与组间差值。
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
from evals.rag.metrics import (
    hit_at_k,
    mean_average_precision,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

DATASET_PATH = "evals/rag/datasets/rag_retrieval_cases.json"
REPORT_STEM = "evals/rag/reports/ablation_retrieval"  # 带戳版 + latest 两份，不覆盖历史

RerankPrefer = Literal["auto", "bge", "llm"]

GROUPS = [
    {"name": "A_dense_only", "enable_hybrid": False, "enable_rerank": False, "prefer": "llm"},
    {"name": "B_hybrid", "enable_hybrid": True, "enable_rerank": False, "prefer": "llm"},
    {"name": "C_dense_llm_rerank", "enable_hybrid": False, "enable_rerank": True, "prefer": "llm"},
    {"name": "D_hybrid_llm_rerank", "enable_hybrid": True, "enable_rerank": True, "prefer": "llm"},
    {"name": "E_dense_bge_rerank", "enable_hybrid": False, "enable_rerank": True, "prefer": "bge"},
    {"name": "F_hybrid_bge_rerank", "enable_hybrid": True, "enable_rerank": True, "prefer": "bge"},
]


async def eval_group(cases, enable_hybrid: bool, enable_rerank: bool, prefer: RerankPrefer = "llm") -> dict:
    """一组配置的检索指标（每次重建 VectorStore 保证开关干净生效）。"""
    _, vector_store = await build_rag(
        enable_hybrid=enable_hybrid,
        enable_rerank=enable_rerank,
        dense_top_k=10,
        rerank_prefer=prefer,
    )
    # BM25 是进程内派生索引（重启即失）——hybrid 组必须先从 Milvus 恢复，
    # 否则 hybrid 名存实亡（RRF 融合空列表，等价 dense-only）。
    restored = await vector_store.restore_bm25_index()
    logger.info(f"BM25 恢复 {restored} 篇")
    h1s, h3s, r3s, mrrs, p3s, ndcgs, preds, golds = [], [], [], [], [], [], [], []
    per_case = []
    for c in cases:
        gold = set(c["gold_sources"])
        docs = await vector_store.search(c["query"], top_k=3)
        pred = [(d.get("metadata") or {}).get("source", "") for d in docs]
        pred = [p for p in pred if p]
        h1, h3 = hit_at_k(pred, gold, 1), hit_at_k(pred, gold, 3)
        rec3 = recall_at_k(pred, gold, 3)
        m = mrr(pred, gold)
        p3, n3 = precision_at_k(pred, gold, 3), ndcg_at_k(pred, gold, 3)
        h1s.append(h1)
        h3s.append(h3)
        r3s.append(rec3)
        mrrs.append(m)
        p3s.append(p3)
        ndcgs.append(n3)
        preds.append(pred)
        golds.append(gold)
        per_case.append({"id": c["id"], "pred": pred, "hit@3": h3, "mrr": m})
    return {
        "summary": {
            "rerank_type": "none" if not enable_rerank else ("bge" if prefer == "bge" else "llm"),
            "hit@1": round(mean(h1s), 4),
            "hit@3": round(mean(h3s), 4),
            "recall@3": round(mean(r3s), 4),
            "mrr": round(mean(mrrs), 4),
            "precision@3": round(mean(p3s), 4),
            "ndcg@3": round(mean(ndcgs), 4),
            "map": round(mean_average_precision(preds, golds), 4),
        },
        "cases": per_case,
    }


async def main():
    parser = argparse.ArgumentParser(description="RAG 检索消融（hybrid/rerank 贡献分解）")
    parser.add_argument("--cases", type=int, default=None, help="只跑前 N 条（抽样冒烟）")
    args = parser.parse_args()

    cases = load_json(DATASET_PATH)
    if args.cases:
        cases = cases[: args.cases]

    results = {}
    for g in GROUPS:
        logger.info(f"=== {g['name']} (hybrid={g['enable_hybrid']}, rerank={g['enable_rerank']}, prefer={g['prefer']})")
        r = await eval_group(cases, g["enable_hybrid"], g["enable_rerank"], prefer=g["prefer"])
        results[g["name"]] = r
        logger.info(str(r["summary"]))

    # 组间差值：相对 dense 基线的提升
    base = {k: v for k, v in results["A_dense_only"]["summary"].items() if isinstance(v, (int, float))}
    deltas = {}
    for name, r in results.items():
        summary = {k: v for k, v in r["summary"].items() if isinstance(v, (int, float))}
        deltas[name] = {k: round(summary[k] - base[k], 4) for k in base}

    report = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "num_cases": len(cases),
            "embedding": f"{get_settings().embedding_provider}/{get_settings().embedding_model}",
            "rerank_llm": get_settings().llm_model,
        },
        "groups": results,
        "delta_vs_dense": deltas,
    }
    stamped = f"{REPORT_STEM}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    save_json(stamped, report)
    save_json(f"{REPORT_STEM}_latest.json", report)  # latest 是戳版副本，历史在带戳文件
    logger.info(f"saved: {stamped} (+latest 副本)")
    logger.info("相对 dense 基线的提升（Hit@3 / MRR）")
    for name, d in deltas.items():
        logger.info(f"  {name:<18} Hit@3 {d['hit@3']:+.4f} | MRR {d['mrr']:+.4f}")


if __name__ == "__main__":
    asyncio.run(main())
