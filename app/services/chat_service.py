# 对话服务（v2 精简版）
# 说明：从 my-agent 的 358 行版本重建。会话摘要仅读取（不再自动生成），
# metadata_filters 和知识库来源由请求级 RAG ContextVar 传递。
from typing import AsyncIterator, Optional

from loguru import logger

from app.agents.chat_agent import ChatAgent
from app.agents.context_trace import context_hits_payload, use_context_trace
from app.datasources.context import use_datasource_graph_scope
from app.datasources.service import normalize_workspace_id
from app.rag.context import current_sources, use_metadata_filters
from app.services.session_store import SessionStore
from app.text2sql.feedback import latest_successful_sql, use_sql_recorder


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
            use_datasource_graph_scope(datasource_id, normalize_workspace_id(workspace_id)),
            use_metadata_filters(metadata_filters),
            use_sql_recorder(question),
            use_context_trace(),
        ):
            answer = await self.agent.chat(question, history=history, summary=summary)
            sources = current_sources()
            sql_result = _sql_result_payload(latest_successful_sql())
            context_hits = context_hits_payload()

        self.session_store.add_message(session_id, "user", question)
        self.session_store.add_message(session_id, "assistant", answer)
        return {"answer": answer, "sources": sources, "sql_result": sql_result, "context_hits": context_hits}

    async def chat_stream(
        self,
        session_id: str,
        question: str,
        metadata_filters: Optional[dict] = None,
        datasource_id: Optional[int] = None,
        workspace_id: Optional[int] = None,
    ) -> AsyncIterator[dict]:
        history = self.session_store.get_history(session_id)
        summary = self.session_store.get_summary(session_id)

        collected_content: list[str] = []
        sources: list[str] = []
        with (
            use_datasource_graph_scope(datasource_id, normalize_workspace_id(workspace_id)),
            use_metadata_filters(metadata_filters),
            use_sql_recorder(question),
            use_context_trace(),
        ):
            async for chunk in self.agent.chat_stream(question, history=history, summary=summary):
                # chat_agent yield {"type": "reasoning"|"content", "text": str}
                text = chunk.get("text", "")
                if text:
                    yield {"type": "content", "data": text}
                if chunk.get("type") == "content":
                    collected_content.append(text)  # 仅最终答案入会话历史
            sources = current_sources()
            sql_result = _sql_result_payload(latest_successful_sql())
            context_hits = context_hits_payload()

        # 只存最终答案（content），思考过程不入历史，保持与非流式 chat() 一致
        answer = "".join(collected_content)
        if answer:
            self.session_store.add_message(session_id, "user", question)
            self.session_store.add_message(session_id, "assistant", answer)
        logger.info(f"流式对话完成 - Session: {session_id}, 长度: {len(answer)}")
        yield {"type": "sources", "data": sources}
        # 本轮若执行过 SQL，随流下发结构化结果（前端「沉淀为示例」用；旧前端忽略未知类型）
        if sql_result is not None:
            yield {"type": "sql_result", "data": sql_result}
        # 本轮若调用过工具，下发检索命中与调用轨迹（前端可解释性面板；旧前端忽略未知类型）
        if context_hits is not None:
            yield {"type": "context_hits", "data": context_hits}


def _sql_result_payload(record) -> Optional[dict]:
    """SQLExecutionRecord → SSE/响应负载；无记录（本轮没执行 SQL）返回 None。"""
    if record is None:
        return None
    return {
        "question": record.question,
        "sql": record.sql,
        "row_count": record.row_count,
        "columns": record.columns,
        "datasource_id": record.datasource_id,
    }
