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

from app.core.dependencies import get_task_service
from app.tasks.service import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskCreateRequest(BaseModel):
    type: str = Field(description="任务类型：chat / eval")
    params: dict = Field(default_factory=dict, description="任务参数，如 {question, session_id} 或 {limit, model}")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(req: TaskCreateRequest, task_service: TaskService = Depends(get_task_service)):
    """提交异步任务，立即返回 task_id（任务在后台 worker 执行）。"""
    try:
        task_id = await task_service.enqueue(req.type, req.params)
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
