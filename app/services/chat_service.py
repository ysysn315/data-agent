# 对话服务（v2 精简版）
# 说明：从 my-agent 的 358 行版本重建。会话摘要仅读取（不再自动生成），
# metadata_filters 和知识库来源由请求级 RAG ContextVar 传递。
from typing import AsyncIterator, Optional

from loguru import logger

from app.agents.chat_agent import ChatAgent
from app.datasources.context import use_datasource
from app.datasources.service import normalize_workspace_id
from app.rag.context import current_sources, use_metadata_filters
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
        datasource_id: Optional[int] = None,
        workspace_id: Optional[int] = None,
    ) -> dict:
        history = self.session_store.get_history(session_id)
        summary = self.session_store.get_summary(session_id)

        with (
            use_datasource(datasource_id, normalize_workspace_id(workspace_id)),
            use_metadata_filters(metadata_filters),
        ):
            answer = await self.agent.chat(question, history=history, summary=summary)
            sources = current_sources()

        self.session_store.add_message(session_id, "user", question)
        self.session_store.add_message(session_id, "assistant", answer)
        return {"answer": answer, "sources": sources}

    async def chat_stream(
        self,
        session_id: str,
        question: str,
        metadata_filters: Optional[dict] = None,
        datasource_id: Optional[int] = None,
        workspace_id: Optional[int] = None,
        include_sources: bool = False,
    ) -> AsyncIterator[str | dict]:
        history = self.session_store.get_history(session_id)
        summary = self.session_store.get_summary(session_id)

        collected_content: list[str] = []
        sources: list[str] = []
        with (
            use_datasource(datasource_id, normalize_workspace_id(workspace_id)),
            use_metadata_filters(metadata_filters),
        ):
            async for chunk in self.agent.chat_stream(question, history=history, summary=summary):
                # chat_agent yield {"type": "reasoning"|"content", "text": str}
                text = chunk.get("text", "")
                if text:
                    # 默认保持原有字符串流；API 路由可选择附带结构化来源事件。
                    yield {"type": "content", "data": text} if include_sources else text
                if chunk.get("type") == "content":
                    collected_content.append(text)  # 仅最终答案入会话历史
            sources = current_sources()

        # 只存最终答案（content），思考过程不入历史，保持与非流式 chat() 一致
        answer = "".join(collected_content)
        if answer:
            self.session_store.add_message(session_id, "user", question)
            self.session_store.add_message(session_id, "assistant", answer)
        logger.info(f"流式对话完成 - Session: {session_id}, 长度: {len(answer)}")
        if include_sources:
            yield {"type": "sources", "data": sources}
