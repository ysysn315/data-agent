# 向量化服务模块 — 统一走 OpenAI 兼容接口
# 解除对 DashScope SDK 的绑定：DashScope compatible-mode / Ollama / OpenAI 均可通过 base_url 接入
from typing import List

from loguru import logger

from app.core.settings import Settings


class EmbeddingService:
    def __init__(self, settings: Settings):
        if settings.embedding_provider == "bge":
            # 本地 BGE 模型（依赖 torch/FlagEmbedding，重依赖，惰性导入）
            from app.rag.bge_embeddings import BGELocalEmbeddings

            self.embeddings = BGELocalEmbeddings(
                model_name=settings.embedding_model,
                device=settings.embedding_device,
            )
        else:
            # OpenAI 兼容接口（openai / ollama / dashscope compatible-mode）
            from langchain_openai import OpenAIEmbeddings

            self.embeddings = OpenAIEmbeddings(
                model=settings.embedding_model,
                api_key=settings.embedding_api_key or "not-set",
                base_url=settings.embedding_base_url,
                # Ollama 等本地服务不支持 tiktoken 预分词
                check_embedding_ctx_length=False,
            )

    async def embed_text(self, text: str) -> List[float]:
        try:
            result = await self.embeddings.aembed_query(text)
            logger.info(f"向量化单个文本成功，文本长度: {len(text)}, 向量维度: {len(result)}")
            return result
        except Exception as e:
            logger.error(f"向量化文本失败: {str(e)}")
            raise Exception(f"向量化失败: {str(e)}")

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        try:
            result = await self.embeddings.aembed_documents(texts)
            logger.info(f"批量向量化成功，文本数量: {len(texts)}, 向量维度: {len(result[0]) if result else 0}")
            return result
        except Exception as e:
            logger.error(f"批量向量化失败: {str(e)}")
            raise Exception(f"批量向量化失败: {str(e)}")
