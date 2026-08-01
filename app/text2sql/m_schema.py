"""M-Schema 生成器（对齐 SQLBot）。

M-Schema 是 SQLBot 注入给 LLM 的表结构描述格式（见
sqlbot-reference/backend/apps/datasource/crud/table.py:72-93、
templates/template.yaml 中 <m-schema> 的说明）。相比原始 DDL，它更紧凑、
把中文注释直接贴在字段旁，利于模型对齐"字段含义 → SQL 标识符"。

本模块从 SQLite 库读取真实结构（PRAGMA table_info），再用注释字典
（comments_ecommerce.ECOMMERCE_COMMENTS 或调用方传入）补齐中文含义，
输出如下格式：

    # Table: orders, 订单表
    [(order_id:TEXT, 订单ID), (customer_id:TEXT, 客户ID), (order_status:TEXT)]

规则：
- 表/字段无注释时，省略 ", 注释" 部分，**绝不编造**（对齐 SQLBot custom_comment 为空即不输出）。
- 类型取 SQLite 声明类型（PRAGMA 的 type 列）；SQLite 时间列通常声明为 TEXT。
- 多张表之间以空行分隔。

与 SQLBot 的差异：SQLBot 每个字段单独一行（多行 M-Schema），这里压成单行
`[(...), (...)]` 更省 token，语义等价；demo 表字段少，可读性无损。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from app.text2sql.comments_ecommerce import ECOMMERCE_COMMENTS


def _column_entry(name: str, col_type: str, comment: str) -> str:
    """构造单个字段项：(字段名:类型) 或 (字段名:类型, 注释)"""
    col_type = (col_type or "").strip().upper() or "UNKNOWN"
    if comment:
        return f"({name}:{col_type}, {comment})"
    return f"({name}:{col_type})"


def build_table_m_schema(
    table_name: str,
    columns: list[tuple[str, str]],
    table_comment: str = "",
    field_comments: Optional[dict[str, str]] = None,
) -> str:
    """把单张表的列信息渲染成 M-Schema 文本。

    参数:
        table_name: 表名
        columns: [(字段名, 类型), ...]，通常来自 PRAGMA table_info
        table_comment: 表注释（空则省略）
        field_comments: {字段名: 注释}，命中才输出注释，未命中不编造
    """
    field_comments = field_comments or {}

    header = f"# Table: {table_name}"
    if table_comment:
        header += f", {table_comment}"

    entries = [_column_entry(name, col_type, field_comments.get(name, "")) for name, col_type in columns]
    body = "[" + ", ".join(entries) + "]"
    return f"{header}\n{body}"


def list_tables(db_path: str) -> list[str]:
    """列出库内所有用户表（排除 sqlite_ 内部表），按表名排序"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _read_columns(conn: sqlite3.Connection, table_name: str) -> list[tuple[str, str]]:
    """PRAGMA table_info 读取字段名与声明类型，保持库内列顺序"""
    # cid, name, type, notnull, dflt_value, pk
    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return [(r[1], r[2]) for r in rows]


def generate_m_schema(
    db_path: str,
    comments: Optional[dict[str, dict]] = None,
    tables: Optional[list[str]] = None,
) -> str:
    """生成整库（或指定表）的 M-Schema 文本。

    参数:
        db_path: SQLite 库文件路径
        comments: 注释字典（结构见 comments_ecommerce.ECOMMERCE_COMMENTS）；
                  None 时默认用演示库注释字典。传 {} 可完全不带注释。
        tables: 只生成这些表；None 表示全库所有表。

    返回:
        多张表拼接的 M-Schema 文本；库不存在时抛 FileNotFoundError（由调用方转提示）。
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")

    if comments is None:
        comments = ECOMMERCE_COMMENTS

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        if tables is None:
            table_names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
        else:
            table_names = tables

        blocks: list[str] = []
        for table_name in table_names:
            columns = _read_columns(conn, table_name)
            if not columns:
                continue
            table_meta = comments.get(table_name, {})
            blocks.append(
                build_table_m_schema(
                    table_name=table_name,
                    columns=columns,
                    table_comment=table_meta.get("comment", ""),
                    field_comments=table_meta.get("fields", {}),
                )
            )
    finally:
        conn.close()

    return "\n\n".join(blocks)
