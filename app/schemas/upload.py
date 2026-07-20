# 上传相关数据模型
# TODO: 任务 13.1 - 定义 UploadResponse 模型
from pydantic import BaseModel,Field
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
