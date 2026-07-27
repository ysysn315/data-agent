# 对话 Agent v2
# 基于 langchain v1 create_agent + 中间件（对齐 Yuxi 架构）：
# - LLM 通过 LLMFactory 注入，不再绑定任何厂商 SDK
# - Skills / 工具熔断等能力通过 AgentMiddleware 挂载
from typing import AsyncIterator, List, Optional, Sequence

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
from loguru import logger

from app.core.tracing import get_langfuse_callbacks

DEFAULT_SYSTEM_PROMPT = (
    "你是一个数据分析助手。优先使用可用的技能（Skills）和工具回答问题；"
    "涉及数据查询时先了解表结构再生成 SQL；"
    "当问题需要最新互联网信息时，可调用 `tavily_search`。"
)


class ChatAgent:
    def __init__(
        self,
        llm: BaseChatModel,
        tools: Sequence,
        middleware: Sequence = (),
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ):
        """
        初始化 ChatAgent。

        参数:
            llm: LLM 实例（由 LLMFactory 创建，任意 OpenAI 兼容接口）
            tools: 基础工具列表（skills 声明的门控工具由 SkillsMiddleware 注册）
            middleware: AgentMiddleware 列表
            system_prompt: 系统提示词
        """
        self.graph = create_agent(
            model=llm,
            tools=list(tools),
            system_prompt=system_prompt,
            middleware=list(middleware),
        )

    def _build_messages(
        self,
        question: str,
        history: Optional[List[dict]] = None,
        summary: str = "",
    ) -> List:
        messages: List = []
        if summary:
            messages.append(
                SystemMessage(content=f"以下是当前会话较早轮次的摘要，请在回答和工具决策时参考：\n{summary}")
            )
        for msg in history or []:
            if msg["role"] == "user":
                messages.append(HumanMessage(content=msg["content"]))
            else:
                messages.append(AIMessage(content=msg["content"]))
        messages.append(HumanMessage(content=question))
        return messages

    async def chat(
        self,
        question: str,
        history: Optional[List[dict]] = None,
        summary: str = "",
    ) -> str:
        """执行一轮对话，返回最终回答文本。"""
        logger.info(f"ChatAgent 收到问题: {question}")
        # callbacks 为空列表时（未启用 Langfuse）行为与未接入完全一致
        result = await self.graph.ainvoke(
            {"messages": self._build_messages(question, history, summary)},
            config={"callbacks": get_langfuse_callbacks()},
        )

        messages = result.get("messages", [])
        if not messages:
            raise RuntimeError(f"Agent 未返回任何消息: {result}")

        answer = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])
        logger.info(f"ChatAgent 回答: {str(answer)[:50]}...")
        return str(answer)

    async def chat_stream(
        self,
        question: str,
        history: Optional[List[dict]] = None,
        summary: str = "",
    ) -> AsyncIterator[str]:
        """流式对话，逐 token 产出模型文本。"""
        async for chunk, _meta in self.graph.astream(
            {"messages": self._build_messages(question, history, summary)},
            stream_mode="messages",
            config={"callbacks": get_langfuse_callbacks()},
        ):
            if isinstance(chunk, (AIMessage, AIMessageChunk)):
                # 优先读 content，如果为空则尝试 reasoning_content（glm-5.2 等推理模型）
                text = ""
                if chunk.content:
                    if isinstance(chunk.content, str):
                        text = chunk.content
                    else:
                        for part in chunk.content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text += part.get("text", "")
                elif hasattr(chunk, "reasoning_content") and getattr(chunk, "reasoning_content"):
                    text = getattr(chunk, "reasoning_content")

                if text:
                    yield text
