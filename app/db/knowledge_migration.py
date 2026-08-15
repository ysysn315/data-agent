"""SQL 知识库（示例/术语表）作用域列的幂等兼容迁移。

背景与 graph_migration.py 相同：项目仍以 ``Base.metadata.create_all`` 起步、
没有 Alembic，``create_all`` 不会给已有表补新列。本轮给 sql_examples / terminology
加作用域列（datasource_id/workspace_id，示例表另有 source/meta），旧数据按默认值
归入演示作用域（datasource_id=NULL、workspace_id=0、source='manual'），行为不变。

与 graph_triples 不同，这里全部是 ADD COLUMN（带 DEFAULT，SQLite/PG 都支持），
不涉及唯一约束变更，无需重建表——主键（example_id / term）不含作用域，
"按 (question, datasource_id) 去重"由领域层内存字典保证（与既有全局去重同机制）。
"""

from __future__ import annotations

from sqlalchemy import inspect

# (表名, 列名, DDL 片段)；缺哪列补哪列，全部幂等
_KNOWLEDGE_COLUMNS: list[tuple[str, str, str]] = [
    ("sql_examples", "datasource_id", "INTEGER"),
    ("sql_examples", "workspace_id", "INTEGER NOT NULL DEFAULT 0"),
    ("sql_examples", "source", "VARCHAR(32) NOT NULL DEFAULT 'manual'"),
    ("sql_examples", "meta", "JSON NOT NULL DEFAULT '{}'"),
    ("terminology", "datasource_id", "INTEGER"),
    ("terminology", "workspace_id", "INTEGER NOT NULL DEFAULT 0"),
]

_KNOWLEDGE_INDEXES: list[tuple[str, str, str]] = [
    ("ix_sql_examples_datasource_id", "sql_examples", "datasource_id"),
    ("ix_terminology_datasource_id", "terminology", "datasource_id"),
]


def upgrade_knowledge_schema(connection) -> None:
    """给旧版 sql_examples / terminology 补作用域列与索引。

    测试和新数据库没有旧表时是 no-op（create_all 随后按新模型建全量列）。
    PG 的 JSON 默认值语法与 SQLite 不同，按方言分支。
    """
    inspector = inspect(connection)
    tables = inspector.get_table_names()
    dialect = connection.dialect.name

    for table, column, ddl in _KNOWLEDGE_COLUMNS:
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column in existing:
            continue
        if column == "meta" and dialect != "sqlite":
            # PG 的 JSON 列默认值需要显式 ::json 转型
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} JSON NOT NULL DEFAULT '{{}}'::json")
        else:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    for index, table, column in _KNOWLEDGE_INDEXES:
        if table in tables:
            connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {index} ON {table}({column})")
