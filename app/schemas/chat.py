# 对话请求/响应模型
# TODO: 任务 5.1 - 使用 Pydantic 定义 ChatRequest 和 ChatResponse
# ChatRequest: Id (str), Question (str)
# ChatResponse: answer (str), sources (list, 可选)
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


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
    Id:str
    Question:str
    metadata_filters: Optional[MetadataFilters] = None


class ChatResponse(BaseModel):
    answer:str
    sources:List[str]=[]
