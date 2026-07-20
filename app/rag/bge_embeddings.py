import asyncio
import os
from pathlib import Path
from typing import Iterable, List

import torch
from FlagEmbedding import FlagModel


# 统一放在 D 盘，避免模型下载到系统盘。
MODEL_CACHE_DIR = Path("D:/AI编程/kiro-place/JAVA-agent/my-agent/models")
os.environ["HF_HOME"] = str(MODEL_CACHE_DIR)
os.environ["TRANSFORMERS_CACHE"] = str(MODEL_CACHE_DIR)

# 支持在 .env 里写短别名，代码里统一解析成真实模型名。
MODEL_ALIASES = {
    "bge-large-zh": "BAAI/bge-large-zh-v1.5",
    "bge-large-zh-v1.5": "BAAI/bge-large-zh-v1.5",
}


class BGELocalEmbeddings:
    """本地 BGE embedding 封装，直接走 FlagEmbedding。"""

    def __init__(self, model_name: str, device: str = ""):
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        self.model_name = MODEL_ALIASES.get(model_name, model_name)
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.use_fp16 = self.device == "cuda"
        self.model = FlagModel(
            self.model_name,
            use_fp16=self.use_fp16,
        )

    def _encode(self, texts: Iterable[str]) -> List[List[float]]:
        vectors = self.model.encode(
            list(texts),
            batch_size=32,
            max_length=512,
        )
        return [list(map(float, vector)) for vector in vectors]

    async def aembed_query(self, text: str):
        vectors = await asyncio.to_thread(self._encode, [text])
        return vectors[0] if vectors else []

    async def aembed_documents(self, texts):
        return await asyncio.to_thread(self._encode, texts)
