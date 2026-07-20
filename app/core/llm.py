"""LLM 工厂 - 支持自定义 base_url 和 api_key，兼容 OpenAI 接口规范"""
from typing import Optional

from langchain_openai import ChatOpenAI
from loguru import logger

from app.core.settings import settings


class LLMFactory:
    """LLM 工厂，统一创建 LLM 实例"""

    @staticmethod
    def create_llm(
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: Optional[float] = None,
        streaming: Optional[bool] = None,
        **kwargs
    ) -> ChatOpenAI:
        """
        创建 LLM 实例

        参数:
            model: 模型名称，默认从配置读取
            api_key: API Key，默认从配置读取
            base_url: 自定义 API 地址（如美团 FRIDAY），默认从配置读取
            temperature: 温度，默认从配置读取
            streaming: 是否流式，默认从配置读取
        """
        return ChatOpenAI(
            model=model or settings.llm_model,
            api_key=api_key or settings.llm_api_key,
            base_url=base_url or settings.llm_base_url,
            temperature=temperature or settings.llm_temperature,
            streaming=streaming or settings.llm_streaming,
            **kwargs
        )

    @staticmethod
    def create_embedding_llm(**kwargs) -> ChatOpenAI:
        """创建 embedding 专用 LLM（如本地 Ollama）"""
        return ChatOpenAI(
            model=settings.embedding_model,
            api_key=settings.embedding_api_key or "ollama",
            base_url=settings.embedding_base_url,
            **kwargs
        )


# 使用示例
# mt_llm = LLMFactory.create_llm(
#     model="glm-5.2",
#     api_key="220641...73",
#     base_url="https://aigc.sankuai.com/v1/openai/native"
# )