"""Text-to-SQL 核心模块。

- m_schema: 从 SQLite 库生成 SQLBot 风格的 M-Schema 表结构描述
- comments_ecommerce: 固定演示库兼容注释字典（用户数据源不走此处）

设计与取舍见 app/text2sql/IMPLEMENTATION.md。
"""

from app.text2sql.m_schema import build_table_m_schema, generate_m_schema, list_tables

__all__ = ["generate_m_schema", "build_table_m_schema", "list_tables"]
