"""数据源接入、结构同步与语义审核 API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import get_admin_user, get_current_user, get_datasource_service
from app.datasources.models import (
    DataSourceConfigError,
    DataSourceConnectionError,
    DataSourceNotFoundError,
    ReviewStatus,
    SemanticDraftError,
)
from app.datasources.service import DataSourceService
from app.schemas.datasource import DataSourceCreate, SemanticDraftRequest, TableReviewRequest

router = APIRouter(prefix="/datasources", tags=["datasources"])


def _workspace(user: dict) -> int | None:
    return user.get("workspace_id")


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DataSourceNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DataSourceConfigError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, (DataSourceConnectionError, SemanticDraftError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="数据源操作失败")


@router.post("", status_code=201)
async def create_datasource(
    request: DataSourceCreate,
    admin: dict = Depends(get_admin_user),
    service: DataSourceService = Depends(get_datasource_service),
):
    try:
        return await service.create(request.model_dump(by_alias=True), _workspace(admin))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("")
async def list_datasources(
    user: dict = Depends(get_current_user),
    service: DataSourceService = Depends(get_datasource_service),
):
    return await service.list_sources(_workspace(user))


@router.get("/{datasource_id}")
async def get_datasource(
    datasource_id: int,
    user: dict = Depends(get_current_user),
    service: DataSourceService = Depends(get_datasource_service),
):
    try:
        return await service.get_source(datasource_id, _workspace(user))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.delete("/{datasource_id}")
async def delete_datasource(
    datasource_id: int,
    admin: dict = Depends(get_admin_user),
    service: DataSourceService = Depends(get_datasource_service),
):
    deleted = await service.delete_source(datasource_id, _workspace(admin))
    if not deleted:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return {"deleted": True}


@router.post("/{datasource_id}/sync")
async def sync_datasource(
    datasource_id: int,
    admin: dict = Depends(get_admin_user),
    service: DataSourceService = Depends(get_datasource_service),
):
    try:
        return await service.sync(datasource_id, _workspace(admin))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/{datasource_id}/semantic-draft")
async def generate_semantic_draft(
    datasource_id: int,
    request: SemanticDraftRequest,
    user: dict = Depends(get_current_user),
    service: DataSourceService = Depends(get_datasource_service),
):
    try:
        return await service.generate_semantic_drafts(
            datasource_id,
            _workspace(user),
            table_ids=request.table_ids,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/{datasource_id}/metadata")
async def get_datasource_metadata(
    datasource_id: int,
    user: dict = Depends(get_current_user),
    service: DataSourceService = Depends(get_datasource_service),
):
    try:
        return await service.get_catalog(datasource_id, _workspace(user))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.put("/{datasource_id}/metadata/{table_id}/review")
async def review_datasource_table(
    datasource_id: int,
    table_id: int,
    request: TableReviewRequest,
    user: dict = Depends(get_current_user),
    service: DataSourceService = Depends(get_datasource_service),
):
    try:
        return await service.review_table(
            datasource_id=datasource_id,
            workspace_id=_workspace(user),
            table_id=table_id,
            decision=ReviewStatus(request.decision),
            table_comment=request.table_comment,
            columns=[column.model_dump() for column in request.columns],
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/{datasource_id}/m-schema")
async def get_datasource_m_schema(
    datasource_id: int,
    include_pending: bool = Query(False, description="仅审核预览可设 true；Agent 固定使用 false"),
    user: dict = Depends(get_current_user),
    service: DataSourceService = Depends(get_datasource_service),
):
    try:
        content = await service.get_m_schema(
            datasource_id,
            _workspace(user),
            include_pending=include_pending,
        )
        return {"datasource_id": datasource_id, "include_pending": include_pending, "m_schema": content}
    except Exception as exc:
        raise _http_error(exc) from exc
