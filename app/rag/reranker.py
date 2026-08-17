import os
from pathlib import Path
from typing import Dict, List

from loguru import logger

from app.core.settings import settings

# 模型缓存目录从配置读取，支持自定义
MODEL_CACHE_DIR = Path(settings.MODEL_CACHE_DIR)
os.environ.setdefault("HF_HOME", str(MODEL_CACHE_DIR))
os.environ.setdefault("TRANSFORMERS_CACHE", str(MODEL_CACHE_DIR))


class BGEReranker:
    """Local BGE reranker with CUDA-aware defaults.

    torch/FlagEmbedding 在 __init__ 内才导入（重依赖惰性化）——未安装时
    import 本模块不再炸，构造时抛 RuntimeError 由调用方回退 LLM 重排。
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-base"):
        try:
            import torch
            from FlagEmbedding import FlagReranker
        except ImportError as e:
            raise RuntimeError(
                "BGE reranker 需要 torch/FlagEmbedding（可选重依赖，未安装）——"
                "uv pip install torch FlagEmbedding 后重试，或回退 LLM 重排"
            ) from e

        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.model_name = model_name
        self.use_fp16 = torch.cuda.is_available()
        self.device = "cuda" if self.use_fp16 else "cpu"

        try:
            self.reranker = FlagReranker(model_name, use_fp16=self.use_fp16)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "BGE reranker init failed "
                f"(model={model_name}, device={self.device}, use_fp16={self.use_fp16}, "
                f"cache_dir={MODEL_CACHE_DIR}). "
                "Make sure the model can be loaded from the local cache or downloaded."
            ) from e

        logger.info(f"BGE reranker initialized: model={model_name}, device={self.device}, use_fp16={self.use_fp16}")

    def rerank(self, query: str, documents: List[Dict], top_k: int = None) -> List[Dict]:
        if not documents:
            return []

        limit = top_k or len(documents)
        pairs = [[query, doc["content"]] for doc in documents]

        try:
            raw_scores = self.reranker.compute_score(pairs)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"BGE reranker scoring failed (model={self.model_name}, device={self.device}).") from e

        if raw_scores and isinstance(raw_scores[0], list):
            scores = [score[0] for score in raw_scores]
        else:
            scores = raw_scores if isinstance(raw_scores, list) else [raw_scores]

        scored_docs = list(zip(documents, scores))
        scored_docs.sort(key=lambda item: item[1], reverse=True)
        result = [doc for doc, _score in scored_docs[:limit]]
        logger.info(f"BGE rerank finished, returning {len(result)} documents")
        return result
