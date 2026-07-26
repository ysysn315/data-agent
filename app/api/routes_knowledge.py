"""SQL 知识库（示例库 + 术语库）- API 路由

运营闭环入口：
- POST /api/sql-examples 即"反馈接口"——答对的 question→SQL 存档，越攒越准。
- POST /api/terminology 维护业务术语口径（GMV/复购率/客单价…）。

依赖注入统一走 app/core/dependencies 的单例（不在本文件内自定义同名依赖，
与 skills/mcp 路由一致，避免遮蔽导致列表恒空）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.dependencies import get_example_store, get_term_store
from app.text2sql.examples import ExampleStore
from app.text2sql.terminology import TermStore

router = APIRouter(tags=["knowledge"])


# ========== 请求/响应模型 ==========

class SQLExampleCreate(BaseModel):
    """新增 SQL 示例（反馈接口）"""
    question: str = Field(..., description="自然语言问题")
    sql: str = Field(..., description="对应的 SQL")
    verified: bool = Field(True, description="是否人工确认结果正确")


class SQLExampleResponse(BaseModel):
    id: str
    question: str
    sql: str
    verified: bool


class TermCreate(BaseModel):
    """新增/更新业务术语"""
    term: str = Field(..., description="术语，如 GMV")
    synonyms: list[str] = Field(default_factory=list, description="同义词列表")
    definition: str = Field("", description="口径定义")
    sql_hint: Optional[str] = Field(None, description="SQL 计算口径提示")


class TermResponse(BaseModel):
    term: str
    synonyms: list[str]
    definition: str
    sql_hint: Optional[str] = None


# ========== SQL 示例库 ==========

@router.get("/sql-examples", response_model=list[SQLExampleResponse])
async def list_sql_examples(store: ExampleStore = Depends(get_example_store)):
    """列出全部 SQL 示例"""
    return store.list()


@router.post(
    "/sql-examples",
    response_model=SQLExampleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sql_example(
    req: SQLExampleCreate, store: ExampleStore = Depends(get_example_store)
):
    """新增一条 SQL 示例（答对的问答入库；同问题则更新）"""
    try:
        return store.add(req.question, req.sql, req.verified)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/sql-examples/{example_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sql_example(
    example_id: str, store: ExampleStore = Depends(get_example_store)
):
    """按 id 删除 SQL 示例"""
    if not store.delete(example_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"SQL 示例不存在: {example_id}"
        )


# ========== 业务术语库 ==========

@router.get("/terminology", response_model=list[TermResponse])
async def list_terms(store: TermStore = Depends(get_term_store)):
    """列出全部业务术语"""
    return store.list()


@router.post(
    "/terminology",
    response_model=TermResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_term(req: TermCreate, store: TermStore = Depends(get_term_store)):
    """新增/更新业务术语（term 为唯一键）"""
    try:
        return store.add(req.term, req.synonyms, req.definition, req.sql_hint)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/terminology/{term}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_term(term: str, store: TermStore = Depends(get_term_store)):
    """按 term 删除业务术语"""
    if not store.delete(term):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"术语不存在: {term}"
        )
