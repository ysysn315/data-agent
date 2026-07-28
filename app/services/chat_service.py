# 对话服务（v2 精简版）
# 说明：从 my-agent 的 358 行版本重建。会话摘要仅读取（不再自动生成），
# metadata_filters 由知识库工具参数承接，二期接入。
from typing import AsyncIterator, Optional

from loguru import logger

from app.agents.chat_agent import ChatAgent
from app.services.session_store import SessionStore


class ChatService:
    def __init__(self, agent: ChatAgent, session_store: SessionStore):
        self.agent = agent
        self.session_store = session_store

    async def chat(
        self,
        session_id: str,
        question: str,
        metadata_filters: Optional[dict] = None,
    ) -> dict:
        history = self.session_store.get_history(session_id)
        summary = self.session_store.get_summary(session_id)

        answer = await self.agent.chat(question, history=history, summary=summary)

        self.session_store.add_message(session_id, "user", question)
        self.session_store.add_message(session_id, "assistant", answer)
        return {"answer": answer, "sources": []}

    async def chat_stream(
        self,
        session_id: str,
        question: str,
        metadata_filters: Optional[dict] = None,
    ) -> AsyncIterator[str]:
        history = self.session_store.get_history(session_id)
        summary = self.session_store.get_summary(session_id)

        collected_content: list[str] = []
        collected_reasoning: list[str] = []
        async for chunk in self.agent.chat_stream(question, history=history, summary=summary):
            # 分离 content 和 reasoning_content，只存 content 到会话历史
            if isinstance(chunk, dict):
                if "content" in chunk:
                    text = chunk.get("content", "")
                    collected_content.append(text)
                    yield text
                elif "reasoning_content" in chunk:
                    text = chunk.get("reasoning_content", "")
                    collected_reasoning.append(text)
                    yield text
            else:
                # 兼容字符串 chunk
                collected_content.append(chunk)
                yield chunk

        # 只把 content（非 reasoning）存入会话历史，保持与非流式 chat() 一致
        answer = "".join(collected_content)
        if answer:
            self.session_store.add_message(session_id, "user", question)
            self.session_store.add_message(session_id, "assistant", answer)
        logger.info(f"流式对话完成 - Session: {session_id}, 长度: {len(answer)}")
