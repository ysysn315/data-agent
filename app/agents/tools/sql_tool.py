# SQL 执行工具 —— sqlite-query 技能声明的门控工具（激活后可见）
# 三层只读防护（层层兜底）：
#   1. 关键词校验 _is_select_only：拦掉首词是写操作 / 分号多语句（快速、无依赖）
#   2. AST 校验 validate_sql（sql_guard）：sqlglot 解析，AST 层判断只读 + 单语句，
#      校验表 / 列存在性，最外层缺 LIMIT 自动补齐
#   3. 引擎级只读：sqlite URI mode=ro，任何漏网的写操作在引擎层直接失败
import json
import re
import sqlite3
from pathlib import Path

from langchain_core.tools import tool
from loguru import logger

from app.agents.tools.sql_guard import validate_sql

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

_COMMENT_PATTERN = re.compile(r"(--[^\n]*|/\*.*?\*/)", re.DOTALL)

# sqlite_master 是元数据表，不在业务 schema 里，但允许模型查它拿表结构，
# 故在 schema 校验时显式放行（否则 validate_sql 会把它当未知表拦下）
_SQLITE_MASTER_COLUMNS = ["type", "name", "tbl_name", "rootpage", "sql"]


def _is_select_only(sql: str) -> bool:
    stripped = _COMMENT_PATTERN.sub(" ", sql).strip().rstrip(";").strip()
    if ";" in stripped:
        return False  # 禁止多语句
    first_word = stripped.split(None, 1)[0].lower() if stripped else ""
    return first_word in ("select", "with")


def _fetch_schema(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """从 sqlite_master + PRAGMA 现取库结构 {表名: [列名, ...]}。

    每次执行前现取、不缓存：演示库可能刚被导入 / 替换，缓存会读到过期结构，
    而单库 PRAGMA 开销极小，现取最简单也最不易出错。
    """
    schema: dict[str, list[str]] = {}
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
    for (name,) in rows:
        if name.startswith("sqlite_"):
            continue
        cols = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        # PRAGMA table_info 每行：(cid, name, type, notnull, dflt_value, pk)
        schema[name] = [c[1] for c in cols]
    # 放行元数据表，供模型查表结构
    schema["sqlite_master"] = list(_SQLITE_MASTER_COLUMNS)
    return schema


def create_execute_sql_tool(db_path: str):
    """创建绑定到指定 SQLite 库的只读 SQL 执行工具"""

    @tool
    def execute_sql(sql: str, limit: int = DEFAULT_LIMIT) -> str:
        """在演示数据库（SQLite）上执行只读 SELECT 查询，返回 JSON 结果。

        约束：仅允许 SELECT / WITH 开头的单条查询；执行前会用 sqlglot 做 AST 级校验
        （语法、只读、表名 / 列名存在性），校验失败会返回中文错误说明，请据此改写 SQL。
        自动 LIMIT：若最外层查询没写 LIMIT，会自动补 `LIMIT 1000` 防止全表扫描
        （你自己写了 LIMIT 则不改动）。limit 参数只控制返回给你的行数，不改写 SQL。
        查表结构可用: SELECT name, sql FROM sqlite_master WHERE type='table'

        参数:
            sql: 要执行的 SELECT 语句
            limit: 最大返回行数（默认 100，上限 1000）
        """
        if not Path(db_path).exists():
            return f"数据库文件不存在: {db_path}（请先导入演示数据集）"
        # 第 1 层：关键词快速拦截（写操作首词 / 多语句）
        if not _is_select_only(sql):
            return "拒绝执行：仅允许单条 SELECT / WITH 查询（禁止增删改）"

        limit = max(1, min(int(limit), MAX_LIMIT))
        try:
            # URI mode=ro：引擎级只读，任何写操作直接报错（第 3 层兜底）
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            try:
                # 第 2 层：AST 级校验（现取 schema，不缓存）
                schema = _fetch_schema(conn)
                result = validate_sql(sql, schema=schema, default_limit=MAX_LIMIT)
                if not result.ok:
                    logger.info(f"SQL 校验失败: {result.error} | sql={sql[:200]}")
                    return f"SQL 校验失败: {result.error}"

                cursor = conn.execute(result.fixed_sql)
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
