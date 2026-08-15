"""SQL 知识库（示例库 + 术语库）- API 路由

运营闭环入口：
- POST /api/sql-examples 即"反馈接口"——答对的 question→SQL 存档，越攒越准。
- POST /api/terminology 维护业务术语口径（GMV/复购率/客单价…）。

依赖注入统一走 app/core/dependencies 的单例（不在本文件内自定义同名依赖，
与 skills/mcp 路由一致，避免遮蔽导致列表恒空）。
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.dependencies import (
    get_current_user,
    get_current_user_optional,
    get_datasource_service,
    get_example_store,
    get_term_store,
)
from app.datasources.models import DataSourceNotFoundError
from app.datasources.service import DataSourceService, normalize_workspace_id
from app.text2sql.examples import ExampleStore
from app.text2sql.terminology import TermStore

router = APIRouter(tags=["knowledge"])

# 写口守卫：auth_enabled=True 时需登录；demo 下 get_current_user 恒放行（见 dependencies.py）。
# 读口（list_*）不挂守卫，保持开放。受保护清单集中在 app/core/auth.PROTECTED_ENDPOINTS。


async def resolve_workspace(
    datasource_id: Optional[int],
    user: dict,
    datasource_service: DataSourceService,
) -> int:
    """解析 workspace 并校验可选数据源归属（跨工作空间 404），写口共用。

    可见性规则（GET/DELETE 同源）：记录写入时统一带 workspace_id（平台数据源级
    记录也带），故"属于当前 workspace"即 workspace_id 相等——demo（ws=0）行为不变。
    """
    workspace_id = normalize_workspace_id(user.get("workspace_id") if user else None)
    if datasource_id is not None:
        try:
            await datasource_service.get_source(datasource_id, workspace_id)
        except DataSourceNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return workspace_id


def _workspace_of(user: dict) -> int:
    return normalize_workspace_id(user.get("workspace_id") if user else None)


# ========== 请求/响应模型 ==========


class SQLExampleCreate(BaseModel):
    """新增 SQL 示例（反馈接口）。datasource_id 有值即数据源级示例（平台数据源场景）。"""

    question: str = Field(..., description="自然语言问题")
    sql: str = Field(..., description="对应的 SQL")
    verified: bool = Field(True, description="是否人工确认结果正确（False=候选，不进 few-shot）")
    datasource_id: Optional[int] = Field(None, description="归属数据源；None=演示库全局作用域")
    source: Literal["manual", "chat"] = Field("manual", description="来源（eval 仅允许 CLI 写入）")
    meta: dict = Field(default_factory=dict, description="附加信息（如转正时保留的评测错误标注）")


class SQLExampleResponse(BaseModel):
    id: str
    question: str
    sql: str
    verified: bool
    datasource_id: Optional[int] = None
    source: str = "manual"
    meta: dict = {}


class TermCreate(BaseModel):
    """新增/更新业务术语"""

    term: str = Field(..., description="术语，如 GMV")
    synonyms: list[str] = Field(default_factory=list, description="同义词列表")
    definition: str = Field("", description="口径定义")
    sql_hint: Optional[str] = Field(None, description="SQL 计算口径提示")
    datasource_id: Optional[int] = Field(None, description="归属数据源；None=演示库全局作用域")


class TermResponse(BaseModel):
    term: str
    synonyms: list[str]
    definition: str
    sql_hint: Optional[str] = None
    datasource_id: Optional[int] = None
    workspace_id: int = 0


# ========== SQL 示例库 ==========


@router.get("/sql-examples", response_model=list[SQLExampleResponse])
async def list_sql_examples(
    store: ExampleStore = Depends(get_example_store),
    user: dict | None = Depends(get_current_user_optional),
):
    """列出当前 workspace 的全部 SQL 示例（跨租户不串；匿名视为 ws=0 只见演示数据）"""
    ws = _workspace_of(user)
    return [r for r in store.list() if int(r.get("workspace_id") or 0) == ws]


@router.post(
    "/sql-examples",
    response_model=SQLExampleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sql_example(
    req: SQLExampleCreate,
    store: ExampleStore = Depends(get_example_store),
    user: dict = Depends(get_current_user),
    datasource_service: DataSourceService = Depends(get_datasource_service),
):
    """新增一条 SQL 示例（答对的问答入库；同作用域同问题则更新）

    datasource_id 有值时校验数据源归属（跨工作空间 404），防把示例挂到别人的数据源下。
    """
    workspace_id = await resolve_workspace(req.datasource_id, user, datasource_service)

    try:
        return store.add(
            req.question,
            req.sql,
            req.verified,
            datasource_id=req.datasource_id,
            workspace_id=workspace_id,
            source=req.source,
            meta=req.meta or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete(
    "/sql-examples/{example_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_current_user)],
)
async def delete_sql_example(
    example_id: str,
    store: ExampleStore = Depends(get_example_store),
    user: dict = Depends(get_current_user),
):
    """按 id 删除 SQL 示例（仅限本 workspace 的记录，跨租户视同不存在）"""
    ws = _workspace_of(user)
    owned = [r for r in store.list() if r["id"] == example_id and int(r.get("workspace_id") or 0) == ws]
    if not owned:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"SQL 示例不存在: {example_id}")
    store.delete(example_id)


# ========== 业务术语库 ==========


@router.get("/terminology", response_model=list[TermResponse])
async def list_terms(
    store: TermStore = Depends(get_term_store),
    user: dict | None = Depends(get_current_user_optional),
):
    """列出当前 workspace 的业务术语（跨租户不串；匿名视为 ws=0 只见演示数据）"""
    ws = _workspace_of(user)
    return [t for t in store.list() if int(t.get("workspace_id") or 0) == ws]


@router.post(
    "/terminology",
    response_model=TermResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_term(
    req: TermCreate,
    store: TermStore = Depends(get_term_store),
    user: dict = Depends(get_current_user),
    datasource_service: DataSourceService = Depends(get_datasource_service),
):
    """新增/更新业务术语（作用域内唯一：同 term 可在不同作用域各自存在）"""
    workspace_id = await resolve_workspace(req.datasource_id, user, datasource_service)

    try:
        return store.add(
            req.term,
            req.synonyms,
            req.definition,
            req.sql_hint,
            datasource_id=req.datasource_id,
            workspace_id=workspace_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete(
    "/terminology/{term}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_current_user)],
)
async def delete_term(
    term: str,
    store: TermStore = Depends(get_term_store),
    user: dict = Depends(get_current_user),
    datasource_id: Optional[int] = None,
):
    """按 (term, 作用域) 删除业务术语——仅限本 workspace，跨租户视同不存在。

    datasource_id 缺省删演示作用域词条；同 term 多作用域时前端按卡片传参。
    """
    ws = _workspace_of(user)
    if not store.delete(term, datasource_id=datasource_id, workspace_id=ws):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"术语不存在: {term}")
