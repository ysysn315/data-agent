"""知识图谱 API：作用域隔离、审核目录同步、实体/路径查询。

写入默认是管理员操作；查询要求当前用户，且可选 datasource_id 必须属于当前
workspace。GraphService 单例不保存租户状态，路由通过 ContextVar 设置请求级
GraphScope。同步 SQLite/NetworkX 门面和 LLM 抽取放进工作线程，避免阻塞 FastAPI
事件循环。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.request_scope import resolve_graph_scope
from app.core.dependencies import get_admin_user, get_current_user, get_datasource_service, get_graph_service
from app.datasources.service import DataSourceService
from app.graph.scope import GraphScope, use_graph_scope
from app.graph.service import GraphService

router = APIRouter(prefix="/graph", tags=["graph"])


# ========== 请求模型 ==========


class TripleIn(BaseModel):
    """一条三元组：主语 -[谓词]-> 宾语"""

    subject: str = Field(..., min_length=1, description="主语（实体/指标名）")
    predicate: str = Field(..., min_length=1, description="谓词（短动词短语，如 属于、计算自）")
    object: str = Field(..., min_length=1, description="宾语（实体/指标名）")
    source: str = Field("manual", description="来源标记（默认 manual）")
    source_type: str | None = Field(None, description="来源分类；缺省沿用 source")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="事实置信度（0~1）")


class TriplesCreate(BaseModel):
    """批量添加三元组"""

    triples: list[TripleIn] = Field(..., min_length=1, description="待入库的三元组列表")


class ExtractRequest(BaseModel):
    """LLM 抽取请求"""

    text: str = Field(..., description="待抽取的文本（空白文本直接返回空结果，不调 LLM）")


class EntityMergeRequest(BaseModel):
    survivor_id: int = Field(..., ge=1)
    duplicate_id: int = Field(..., ge=1)


async def _resolve_scope(
    datasource_id: int | None,
    user: dict,
    datasource_service: DataSourceService,
) -> GraphScope:
    return await resolve_graph_scope(datasource_id, user, datasource_service)


# ========== 写入 ==========


@router.post("/triples", status_code=status.HTTP_201_CREATED)
async def add_triples(
    req: TriplesCreate,
    datasource_id: int | None = Query(None, ge=1),
    admin: dict = Depends(get_admin_user),
    datasource_service: DataSourceService = Depends(get_datasource_service),
    service: GraphService = Depends(get_graph_service),
) -> dict:
    """手动添加三元组（(s,p,o) 幂等：已存在的跳过，返回 added/skipped/total）"""
    scope = await _resolve_scope(datasource_id, admin, datasource_service)
    with use_graph_scope(scope):
        return await asyncio.to_thread(service.add_triples, [t.model_dump() for t in req.triples])


@router.post("/extract")
async def extract_from_text(
    req: ExtractRequest,
    datasource_id: int | None = Query(None, ge=1),
    admin: dict = Depends(get_admin_user),
    datasource_service: DataSourceService = Depends(get_datasource_service),
    service: GraphService = Depends(get_graph_service),
) -> dict:
    """调用 LLM 从文本抽取三元组并入库，返回抽取结果与实际新增数"""
    try:
        scope = await _resolve_scope(datasource_id, admin, datasource_service)
        with use_graph_scope(scope):
            return await asyncio.to_thread(service.extract_and_add, req.text)
    except ValueError as e:
        # LLM 未配置（LLM_API_KEY 缺失）等显式配置错误 → 503，提示补配置而不是假装成功
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)) from e


@router.post("/entities/merge")
async def merge_entities(
    req: EntityMergeRequest,
    datasource_id: int | None = Query(None, ge=1),
    admin: dict = Depends(get_admin_user),
    datasource_service: DataSourceService = Depends(get_datasource_service),
    service: GraphService = Depends(get_graph_service),
) -> dict:
    """管理员确认两个同作用域实体为同一实体，保留属性/关系和合并状态。"""

    scope = await _resolve_scope(datasource_id, admin, datasource_service)
    try:
        with use_graph_scope(scope):
            return await asyncio.to_thread(service.merge_entities, req.survivor_id, req.duplicate_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/sync-catalog")
async def sync_catalog(
    catalog: dict,
    datasource_id: int = Query(..., ge=1),
    admin: dict = Depends(get_admin_user),
    datasource_service: DataSourceService = Depends(get_datasource_service),
    service: GraphService = Depends(get_graph_service),
) -> dict:
    """将已审核数据源目录显式同步为图谱实体和关系。"""

    scope = await _resolve_scope(datasource_id, admin, datasource_service)
    with use_graph_scope(scope):
        return await asyncio.to_thread(service.sync_catalog, catalog)


# ========== 查询 ==========


@router.get("/entity/{name}")
async def get_entity(
    name: str,
    depth: int = Query(1, ge=1, le=5, description="邻居深度（1=直接相邻）"),
    datasource_id: int | None = Query(None, ge=1),
    user: dict = Depends(get_current_user),
    datasource_service: DataSourceService = Depends(get_datasource_service),
    service: GraphService = Depends(get_graph_service),
) -> dict:
    """实体的邻居子图（出边入边都带谓词；实体不存在 → 404）"""
    scope = await _resolve_scope(datasource_id, user, datasource_service)
    with use_graph_scope(scope):
        result = await asyncio.to_thread(service.query_entity, name, depth)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"图谱中不存在实体: {name}")
    return result


@router.get("/path")
async def get_path(
    from_entity: str = Query(..., alias="from", description="起点实体"),
    to_entity: str = Query(..., alias="to", description="终点实体"),
    max_hops: int = Query(5, ge=1, le=5, description="最大路径跳数"),
    datasource_id: int | None = Query(None, ge=1),
    user: dict = Depends(get_current_user),
    datasource_service: DataSourceService = Depends(get_datasource_service),
    service: GraphService = Depends(get_graph_service),
) -> dict:
    """两实体间最短路（无向可达，逐跳保留真实方向与谓词链；端点不存在 → 404）"""
    scope = await _resolve_scope(datasource_id, user, datasource_service)
    with use_graph_scope(scope):
        result = await service.find_path_resolved(from_entity, to_entity, max_hops=max_hops)
    if result.get("status") == "ambiguous":
        return result
    if result["missing"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"图谱中不存在实体: {'、'.join(result['missing'])}",
        )
    return result


@router.get("/stats")
async def get_stats(
    datasource_id: int | None = Query(None, ge=1),
    user: dict = Depends(get_current_user),
    datasource_service: DataSourceService = Depends(get_datasource_service),
    service: GraphService = Depends(get_graph_service),
) -> dict:
    """图谱统计（实体数 / 三元组数 / 谓词分布 / 来源分布）"""
    scope = await _resolve_scope(datasource_id, user, datasource_service)
    with use_graph_scope(scope):
        return await asyncio.to_thread(service.stats)
