"""异步任务 - API 路由

    POST /api/tasks            提交任务 {type, params} -> {task_id}
    GET  /api/tasks/{id}       查询状态 + 结果
    GET  /api/tasks/{id}/events SSE 事件流（data: {...}，任务结束发 done 后关闭）

路由风格对齐 routes_mcp（APIRouter(prefix=...) + Depends 注入 service），SSE 复用
routes_chat 的 StreamingResponse(text/event-stream)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.request_scope import resolve_graph_scope
from app.core.dependencies import get_current_user_optional, get_datasource_service, get_task_service
from app.datasources.service import DataSourceService, normalize_workspace_id
from app.tasks.service import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskCreateRequest(BaseModel):
    type: str = Field(description="任务类型：chat / eval / run_analysis_task")
    params: dict = Field(
        default_factory=dict,
        description="任务参数，如 {question, session_id, datasource_id} 或 {limit, model}",
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(
    req: TaskCreateRequest,
    task_service: TaskService = Depends(get_task_service),
    user: dict | None = Depends(get_current_user_optional),
    datasource_service: DataSourceService = Depends(get_datasource_service),
):
    """提交异步任务，立即返回 task_id（任务在后台 worker 执行）。"""
    params = dict(req.params)
    workspace_id = normalize_workspace_id(user.get("workspace_id") if user else None)
    scoped_task = req.type in {"chat", "run_analysis_task"}
    if scoped_task:
        params["workspace_id"] = workspace_id  # 不信任客户端提交的 workspace_id
    else:
        params.pop("workspace_id", None)
    if params.get("datasource_id") is not None:
        if not scoped_task:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"任务类型 {req.type} 不支持 datasource_id",
            )
        try:
            datasource_id = int(params["datasource_id"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="datasource_id 必须是整数",
            ) from exc
        scope = await resolve_graph_scope(datasource_id, user, datasource_service)
        workspace_id = scope.workspace_id
        params["workspace_id"] = workspace_id
        params["datasource_id"] = datasource_id
    try:
        task_id = await task_service.enqueue(req.type, params)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"task_id": task_id}


@router.get("/{task_id}")
async def get_task(task_id: str, task_service: TaskService = Depends(get_task_service)):
    """查询任务状态与结果。"""
    status_doc = await task_service.get_status(task_id)
    if status_doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务不存在: {task_id}")
    return status_doc


@router.get("/{task_id}/events")
async def stream_task_events(task_id: str, task_service: TaskService = Depends(get_task_service)):
    """订阅任务进度事件流（SSE）。任务结束发 done 事件后服务端主动关闭。"""
    return StreamingResponse(
        task_service.stream_sse(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
