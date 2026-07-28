"""LLM 工厂 - 支持自定义 base_url 和 api_key，兼容 OpenAI 接口规范

特殊处理：
- glm-5.2 等推理模型的 reasoning_content 转 content（LangChain 默认只读 content）
"""
from typing import Any, Optional

from langchain_openai import ChatOpenAI
from loguru import logger

from app.core.settings import settings


class ReasoningChatOpenAI(ChatOpenAI):
    """兼容推理模型的 ChatOpenAI

    glm-5.2 等推理模型返回 reasoning_content（思考过程）和 content（最终答案），
    但 LangChain 默认只读 content。当 content 为空时，自动把 reasoning_content
    转成 content，保证下游能拿到文本。
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ):
        """重写：把 reasoning_content 转成 content（流式路径）"""
        # 先手动提取 reasoning_content
        choices = chunk.get("choices", [])
        if choices and choices[0] is not None:
            delta = choices[0].get("delta") or {}
            reasoning_content = delta.get("reasoning_content")
            if reasoning_content and not delta.get("content"):
                # 只在 content 为空时，把 reasoning_content 转成 content
                delta["content"] = reasoning_content
                # 移除 reasoning_content 避免重复
                del delta["reasoning_content"]

        return super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )

    def _generate(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ):
        """重写：非流式路径，把 reasoning_content 转成 content"""
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

        # 处理非流式响应
        for generation in result.generations:
            message = generation.message
            if not message.content and hasattr(message, "additional_kwargs"):
                reasoning = message.additional_kwargs.get("reasoning_content", "")
                if reasoning:
                    message.content = reasoning

        return result

    async def _agenerate(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ):
        """重写：非流式异步路径，把 reasoning_content 转成 content"""
        result = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

        # 处理非流式响应
        for generation in result.generations:
            message = generation.message
            if not message.content and hasattr(message, "additional_kwargs"):
                reasoning = message.additional_kwargs.get("reasoning_content", "")
                if reasoning:
                    message.content = reasoning

        return result

    def _stream(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ):
        """重写：同步流式路径，确保 reasoning_content 被处理"""
        # 父类的 _stream 会调用 _convert_chunk_to_generation_chunk，已重写
        return super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _astream(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ):
        """重写：异步流式路径，确保 reasoning_content 被处理"""
        # 父类的 _astream 会调用 _convert_chunk_to_generation_chunk，已重写
        async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield chunk


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
        resolved_api_key = api_key or settings.llm_api_key
        if not resolved_api_key:
            # 显式失败：不要让空 key 在第一次真实调用时才以 401 暴露
            raise ValueError("LLM_API_KEY 未配置，请在 .env 中设置")

        # 判断是否是推理模型（glm-5.2 / deepseek-r1 / o1 / o3 等）
        model_name = model or settings.llm_model
        reasoning_models = getattr(settings, "reasoning_models", ["glm", "deepseek-r1", "o1", "o3"])
        is_reasoning_model = any(rm in model_name.lower() for rm in reasoning_models)

        llm_class = ReasoningChatOpenAI if is_reasoning_model else ChatOpenAI

        return llm_class(
            model=model_name,
            api_key=resolved_api_key,
            base_url=base_url or settings.llm_base_url,
            temperature=temperature if temperature is not None else settings.llm_temperature,
            streaming=streaming if streaming is not None else settings.llm_streaming,
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