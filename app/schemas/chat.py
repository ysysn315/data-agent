"""对话请求/响应模型。

ChatRequest 可选绑定平台 datasource_id；未提供时保持演示库兼容路径。
"""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class MetadataFilters(BaseModel):
    source: Optional[str] = None
    title: Optional[str] = None
    title_contains: Optional[str] = None
    doc_type: Optional[str] = None
    sheet_name: Optional[str] = None
    section_path: Optional[str] = None
    section_path_contains: Optional[str] = None
    timestamp: Optional[int] = None
    ingested_at_from: Optional[int] = None
    ingested_at_to: Optional[int] = None
    timestamp_from: Optional[int] = None
    timestamp_to: Optional[int] = None

    model_config = ConfigDict(extra="forbid")


class ChatRequest(BaseModel):
    Id: str
    Question: str
    metadata_filters: Optional[MetadataFilters] = None
    datasource_id: Optional[int] = Field(default=None, ge=1)


class SQLResultPayload(BaseModel):
    """本轮对话最后一次成功执行的 SQL（供前端「沉淀为示例」）。"""

    question: str
    sql: str
    row_count: int
    columns: List[str] = []
    datasource_id: Optional[int] = None


class ChatResponse(BaseModel):
    answer: str
    sources: List[str] = []
    sql_result: Optional[SQLResultPayload] = None
