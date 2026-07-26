# 时间工具 - 获取当前日期和时间
from datetime import datetime

from langchain_core.tools import tool


@tool
def get_current_datetime():
    """获取现在的时间和日期"""
    return datetime.now().isoformat()
