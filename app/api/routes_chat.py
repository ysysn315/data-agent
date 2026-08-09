# 对话 API 路由
# v2：ChatService 基于 LLMFactory + create_agent（不再依赖 DashScope SDK）
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger

from app.api.routes_session import get_session_store
from app.core.dependencies import get_chat_agent, get_current_user_optional, get_datasource_service
from app.core.settings import settings
from app.datasources.models import DataSourceNotFoundError
from app.datasources.service import DataSourceService
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import ChatService
from app.services.session_store import SessionStore

router = APIRouter(tags=["chat"])


async def get_chat_service(
    session_store: SessionStore = Depends(get_session_store),
) -> ChatService:
    agent = await get_chat_agent()
    return ChatService(agent, session_store)


async def _workspace_for_request(
    request: ChatRequest,
    user: dict | None,
    datasource_service: DataSourceService,
) -> int | None:
    if request.datasource_id is None:
        return user.get("workspace_id") if user else None
    if settings.auth_enabled and user is None:
        raise HTTPException(status_code=401, detail="选择数据源时需要有效的 API Key")
    workspace_id = user.get("workspace_id") if user else None
    try:
        await datasource_service.get_source(request.datasource_id, workspace_id)
    except DataSourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return workspace_id


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    chat_service: ChatService = Depends(get_chat_service),
    user: dict | None = Depends(get_current_user_optional),
    datasource_service: DataSourceService = Depends(get_datasource_service),
):
    try:
        logger.info(f"收到对话请求 - Session: {request.Id}")
        workspace_id = await _workspace_for_request(request, user, datasource_service)
        metadata_filters = request.metadata_filters.model_dump(exclude_none=True) if request.metadata_filters else None
        answer = await chat_service.chat(
            request.Id,
            request.Question,
            metadata_filters=metadata_filters,
            datasource_id=request.datasource_id,
            workspace_id=workspace_id,
        )
        return ChatResponse(answer=answer["answer"], sources=answer["sources"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"对话请求失败：{e}")
        raise HTTPException(status_code=500, detail=f"对话失败：{str(e)}")


@router.post("/chat_stream")
async def chat_stream(
    request: ChatRequest,
    chat_service: ChatService = Depends(get_chat_service),
    user: dict | None = Depends(get_current_user_optional),
    datasource_service: DataSourceService = Depends(get_datasource_service),
):
    workspace_id = await _workspace_for_request(request, user, datasource_service)

    async def generate():
        try:
            logger.info(f"收到流式对话请求 - Session: {request.Id}")
            metadata_filters = (
                request.metadata_filters.model_dump(exclude_none=True) if request.metadata_filters else None
            )
            async for chunk in chat_service.chat_stream(
                request.Id,
                request.Question,
                metadata_filters=metadata_filters,
                datasource_id=request.datasource_id,
                workspace_id=workspace_id,
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
        },
    )
