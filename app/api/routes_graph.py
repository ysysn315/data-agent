"""知识图谱 - API 路由（E 轮）

入口分两类：
- 写入：POST /api/graph/triples 手动补录；POST /api/graph/extract 由 LLM 从文本
  抽取入库（**显式触发**，不自动挂在文档上传链路上——成本可控性的取舍见
  app/graph/IMPLEMENTATION.md §4）
- 查询：GET /api/graph/entity/{name}（邻居子图）、/api/graph/path（最短路+谓词链）、
  /api/graph/stats（统计）

依赖注入统一走 app/core/dependencies.get_graph_service 单例（与 knowledge/skills
路由同规矩，不在本文件自定义同名依赖）。路由函数用同步 def：GraphService 是同步
门面（run_sync 桥 + LLM 同步调用），FastAPI 会把同步路由放进线程池执行，
LLM 抽取的秒级耗时不会阻塞主事件循环。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.dependencies import get_graph_service
from app.graph.service import GraphService

router = APIRouter(prefix="/graph", tags=["graph"])


# ========== 请求模型 ==========

class TripleIn(BaseModel):
    """一条三元组：主语 -[谓词]-> 宾语"""
    subject: str = Field(..., min_length=1, description="主语（实体/指标名）")
    predicate: str = Field(..., min_length=1, description="谓词（短动词短语，如 属于、计算自）")
    object: str = Field(..., min_length=1, description="宾语（实体/指标名）")
    source: str = Field("manual", description="来源标记（默认 manual）")


class TriplesCreate(BaseModel):
    """批量添加三元组"""
    triples: list[TripleIn] = Field(..., min_length=1, description="待入库的三元组列表")


class ExtractRequest(BaseModel):
    """LLM 抽取请求"""
    text: str = Field(..., description="待抽取的文本（空白文本直接返回空结果，不调 LLM）")


# ========== 写入 ==========

@router.post("/triples", status_code=status.HTTP_201_CREATED)
def add_triples(req: TriplesCreate, service: GraphService = Depends(get_graph_service)) -> dict:
    """手动添加三元组（(s,p,o) 幂等：已存在的跳过，返回 added/skipped/total）"""
    return service.add_triples([t.model_dump() for t in req.triples])


@router.post("/extract")
def extract_from_text(req: ExtractRequest, service: GraphService = Depends(get_graph_service)) -> dict:
    """调用 LLM 从文本抽取三元组并入库，返回抽取结果与实际新增数"""
    try:
        return service.extract_and_add(req.text)
    except ValueError as e:
        # LLM 未配置（LLM_API_KEY 缺失）等显式配置错误 → 503，提示补配置而不是假装成功
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


# ========== 查询 ==========

@router.get("/entity/{name}")
def get_entity(
    name: str,
    depth: int = Query(1, ge=1, le=5, description="邻居深度（1=直接相邻）"),
    service: GraphService = Depends(get_graph_service),
) -> dict:
    """实体的邻居子图（出边入边都带谓词；实体不存在 → 404）"""
    result = service.query_entity(name, depth=depth)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"图谱中不存在实体: {name}"
        )
    return result


@router.get("/path")
def get_path(
    from_entity: str = Query(..., alias="from", description="起点实体"),
    to_entity: str = Query(..., alias="to", description="终点实体"),
    service: GraphService = Depends(get_graph_service),
) -> dict:
    """两实体间最短路（无向可达，逐跳保留真实方向与谓词链；端点不存在 → 404）"""
    result = service.find_path(from_entity, to_entity)
    if result["missing"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"图谱中不存在实体: {'、'.join(result['missing'])}",
        )
    return result


@router.get("/stats")
def get_stats(service: GraphService = Depends(get_graph_service)) -> dict:
    """图谱统计（实体数 / 三元组数 / 谓词分布 / 来源分布）"""
    return service.stats()
