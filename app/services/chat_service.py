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
        async for chunk in self.agent.chat_stream(question, history=history, summary=summary):
            # chat_agent yield {"type": "reasoning"|"content", "text": str}
            if isinstance(chunk, dict):
                text = chunk.get("text", "")
                if text:
                    yield text  # 思考与答案都推给前端展示
                if chunk.get("type") == "content":
                    collected_content.append(text)  # 仅最终答案入会话历史
            else:
                # 兼容裸字符串 chunk
                collected_content.append(chunk)
                yield chunk

        # 只存最终答案（content），思考过程不入历史，保持与非流式 chat() 一致
        answer = "".join(collected_content)
        if answer:
            self.session_store.add_message(session_id, "user", question)
            self.session_store.add_message(session_id, "assistant", answer)
        logger.info(f"流式对话完成 - Session: {session_id}, 长度: {len(answer)}")
