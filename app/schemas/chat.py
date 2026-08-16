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


class TermHitPayload(BaseModel):
    hit_key: str
    rank: int
    term: str
    definition: str = ""


class ExampleHitPayload(BaseModel):
    hit_key: str
    rank: int
    question: str
    sql: str


class DocHitPayload(BaseModel):
    hit_key: str
    rank: int
    source: str
    title: str = ""
    snippet: str = ""


class GraphHitPayload(BaseModel):
    hit_key: str
    rank: int
    kind: str
    query: str
    summary: str = ""
    result_count: int = 0


class HitsPayload(BaseModel):
    """单次工具调用的命中明细。"""

    terms: List[TermHitPayload] = []
    examples: List[ExampleHitPayload] = []
    docs: List[DocHitPayload] = []
    graph: List[GraphHitPayload] = []


class ToolCallPayload(BaseModel):
    """一次工具调用的轨迹（args 已脱敏截断；错误只有稳定枚举与安全文案）。"""

    call_id: str
    name: str
    args: str = ""
    duration_ms: Optional[int] = None
    status: str = "running"
    attempts: Optional[int] = None
    error_code: Optional[str] = None
    public_message: Optional[str] = None
    hits: HitsPayload = HitsPayload()


class ContextHitsPayload(BaseModel):
    """本轮对话的工具调用与检索命中轨迹（可解释性面板）。"""

    tool_calls: List[ToolCallPayload] = []
    summary: dict = {}
    truncated: bool = False


class ChatResponse(BaseModel):
    answer: str
    sources: List[str] = []
    sql_result: Optional[SQLResultPayload] = None
    context_hits: Optional[ContextHitsPayload] = None
