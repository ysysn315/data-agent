# 时间工具 - 获取当前日期和时间
# TODO: 实现 get_current_datetime 工具
from langchain.tools import tool
from datetime import  datetime
from app.rag.vector_store import VectorStore
from typing import Optional
@tool
def get_current_datetime():
    """
    获取现在的时间和日期
    """
    return datetime.now().isoformat()




