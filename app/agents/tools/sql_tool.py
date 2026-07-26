# SQL 执行工具 —— sqlite-query 技能声明的门控工具（激活后可见）
# 只读强制：sqlite URI mode=ro 引擎级只读 + SELECT/WITH 关键词校验（双保险）
import json
import re
import sqlite3
from pathlib import Path

from langchain_core.tools import tool
from loguru import logger

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

_COMMENT_PATTERN = re.compile(r"(--[^\n]*|/\*.*?\*/)", re.DOTALL)


def _is_select_only(sql: str) -> bool:
    stripped = _COMMENT_PATTERN.sub(" ", sql).strip().rstrip(";").strip()
    if ";" in stripped:
        return False  # 禁止多语句
    first_word = stripped.split(None, 1)[0].lower() if stripped else ""
    return first_word in ("select", "with")


def create_execute_sql_tool(db_path: str):
    """创建绑定到指定 SQLite 库的只读 SQL 执行工具"""

    @tool
    def execute_sql(sql: str, limit: int = DEFAULT_LIMIT) -> str:
        """在演示数据库（SQLite）上执行只读 SELECT 查询，返回 JSON 结果。

        约束：仅允许 SELECT / WITH 开头的单条查询；结果默认最多 100 行。
        查表结构可用: SELECT name, sql FROM sqlite_master WHERE type='table'

        参数:
            sql: 要执行的 SELECT 语句
            limit: 最大返回行数（默认 100，上限 1000）
        """
        if not Path(db_path).exists():
            return f"数据库文件不存在: {db_path}（请先导入演示数据集）"
        if not _is_select_only(sql):
            return "拒绝执行：仅允许单条 SELECT / WITH 查询（禁止增删改）"

        limit = max(1, min(int(limit), MAX_LIMIT))
        try:
            # URI mode=ro：引擎级只读，任何写操作直接报错
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            try:
                cursor = conn.execute(sql)
                columns = [d[0] for d in cursor.description] if cursor.description else []
                rows = cursor.fetchmany(limit)
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning(f"SQL 执行失败: {e} | sql={sql[:200]}")
            return f"SQL 执行失败: {e}"

        return json.dumps(
            {"columns": columns, "rows": [list(r) for r in rows], "row_count": len(rows)},
            ensure_ascii=False,
            default=str,
        )

    return execute_sql
