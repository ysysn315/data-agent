"""LLM 工厂 - 支持自定义 base_url 和 api_key，兼容 OpenAI 接口规范

特殊处理：
- glm-5.2 等推理模型的 reasoning_content 转 content（LangChain 默认只读 content）
"""
from typing import Optional

from langchain_openai import ChatOpenAI
from loguru import logger

from app.core.settings import settings


class ReasoningChatOpenAI(ChatOpenAI):
    """兼容推理模型的 ChatOpenAI

    glm-5.2 等推理模型在响应里带 reasoning_content（思考过程）和 content（最终答案），
    但 langchain_openai 1.4.1 明确不提取 reasoning_content（base.py 模块 docstring：
    "not extracted or preserved"）。本子类在两个入口补回：

    - 流式（_convert_chunk_to_generation_chunk）：把 reasoning_content 保留到
      message.additional_kwargs，**不并入 content**——思考过程只展示给用户看，
      不应污染会话历史。下游 ChatAgent.chat_stream 按通道分别读取。
    - 非流式（_create_chat_result）：从原始响应 message.reasoning_content 恢复。
      content 为空（纯思考 / 被 max_tokens 截断）时用它兜底，避免下游拿到空串
      （AnalysisAgent JSON 解析、eval 会因此失败）。

    _create_chat_result 被 _generate / _agenerate 共用，覆盖一处即覆盖同步与异步。
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ):
        """流式：reasoning_content 保留到 additional_kwargs，不并入 content"""
        gen_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if gen_chunk is None:
            # delta=None 等情况父类返回 None
            return None
        # 兼容 beta.chat.completions.stream 的 choices 包装（chunk.chunk.choices）
        choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
        if choices and choices[0] is not None:
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if reasoning:
                gen_chunk.message.additional_kwargs["reasoning_content"] = reasoning
        return gen_chunk

    def _create_chat_result(
        self,
        response,
        generation_info: dict | None = None,
    ):
        """非流式：从原始响应恢复 reasoning_content（langchain 默认丢弃）"""
        result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response
            if isinstance(response, dict)
            else response.model_dump(warnings=False)
        )
        raw_choices = response_dict.get("choices", []) or []
        for gen, raw_choice in zip(result.generations, raw_choices):
            raw_msg = raw_choice.get("message", {}) if isinstance(raw_choice, dict) else {}
            reasoning = raw_msg.get("reasoning_content") or raw_msg.get("reasoning")
            if reasoning and not gen.message.content:
                # content 为空（纯思考 / 被截断）时用 reasoning 兜底，避免下游空串失败
                gen.message.content = reasoning
        return result


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
        reasoning_models = settings.reasoning_models
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