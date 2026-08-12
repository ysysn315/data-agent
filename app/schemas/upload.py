# 上传相关数据模型
# TODO: 任务 13.1 - 定义 UploadResponse 模型
from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    """
    文件上传响应模型

    Example:
        {
            "filename": "document.txt",
            "chunks": 5,
            "status": "success"
        }
    """

    filename: str = Field(..., description="上传的文件名")
    chunks: int = Field(..., description="文档切分的 chunk 数量")
    status: str = Field(..., description="处理状态: success 或 failed")


class DocumentResponse(BaseModel):
    """Milvus 中按 source 聚合后的真实文档索引。"""

    source: str
    title: str
    doc_type: str
    chunk_count: int
    ingested_at: int | None = None
    sheet_names: list[str] = Field(default_factory=list)
