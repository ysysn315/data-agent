# 对话 API 路由
# TODO: 任务 5.3 - 实现对话端点
# - POST /api/chat - 基础对话端点
# - 使用 FastAPI APIRouter
# - 使用 Depends() 进行依赖注入
# - 处理 ChatRequest 并返回 ChatResponse
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.session_store import SessionStore
from app.services.chat_service import ChatService
from app.clients.dashscope_client import DashScopeClient
from app.core.settings import Settings, get_settings
from app.api.routes_session import get_session_store
from loguru import logger
import json
router = APIRouter(tags=["chat"])


def get_chat_service(settings: Settings = Depends(get_settings),
                     session_store: SessionStore = Depends(get_session_store)) -> ChatService:

    return ChatService(settings, session_store)


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, chat_service: ChatService = Depends(get_chat_service)):
    try:
        logger.info(f"收到对话请求 - Session: {request.Id}")
        metadata_filters = (
            request.metadata_filters.model_dump(exclude_none=True)
            if request.metadata_filters else None
        )
        answer = await chat_service.chat(
            request.Id,
            request.Question,
            metadata_filters=metadata_filters,
        )
        return ChatResponse(
            answer=answer["answer"],
            sources=answer["sources"]
        )
    except Exception as e:
        logger.error(f"对话请求失败：{e}")
        raise HTTPException(
            status_code=500,
            detail=f"对话失败：{str(e)}"
        )
@router.post("/chat_stream")
async def chat_stream(
        request: ChatRequest,
        chat_service: ChatService = Depends(get_chat_service)
):
    async def generate():
        try:
            logger.info(f"收到流式对话请求 - Session: {request.Id}")
            metadata_filters = (
                request.metadata_filters.model_dump(exclude_none=True)
                if request.metadata_filters else None
            )
            async for chunk in chat_service.chat_stream(
                request.Id,
                request.Question,
                metadata_filters=metadata_filters,
            ):
                yield f"data: {json.dumps({'type': 'content', 'data': chunk}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            logger.error(f"流式对话失败: {str(e)}")
            yield f"data: {json.dumps({'type': 'error', 'data': str(e)}, ensure_ascii=False)}\n\n"
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }

    )

