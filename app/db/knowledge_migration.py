"""SQL 知识库（示例/术语表）作用域列的幂等兼容迁移。

背景与 graph_migration.py 相同：项目仍以 ``Base.metadata.create_all`` 起步、
没有 Alembic，``create_all`` 不会给已有表补新列。本轮给 sql_examples / terminology
加作用域列（datasource_id/workspace_id，示例表另有 source/meta），旧数据按默认值
归入演示作用域（datasource_id=NULL、workspace_id=0、source='manual'），行为不变。

术语表额外走一步重建：旧版 term 是主键（全局唯一），作用域化后唯一键改为
(term, datasource_id, workspace_id)——同一术语可在不同作用域各自存在。SQLite
不能删主键约束，参照 graph_migration 的临时表复制；PG 则 drop 旧主键再建唯一约束。
主键换成自增 id，与 graph_triples 同款。
"""

from __future__ import annotations

from contextlib import suppress

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


def _terminology_pk_is_term(inspector, tables: list[str]) -> bool:
    """旧版 terminology：term 是主键（作用域化前），需要重建表。"""
    if "terminology" not in tables:
        return False
    pk = inspector.get_pk_constraint("terminology").get("constrained_columns") or []
    return pk == ["term"]


def _rebuild_terminology_sqlite(connection) -> None:
    """SQLite：主键不能删，临时表复制（同 graph_migration 的做法）。"""
    connection.exec_driver_sql(
        """
        CREATE TABLE terminology_v2 (
            id INTEGER NOT NULL PRIMARY KEY,
            term VARCHAR(256) NOT NULL,
            synonyms JSON NOT NULL,
            definition TEXT NOT NULL,
            sql_hint TEXT,
            datasource_id INTEGER,
            workspace_id INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO terminology_v2 (term, synonyms, definition, sql_hint, datasource_id, workspace_id, created_at)
        SELECT term, synonyms, definition, sql_hint, datasource_id, workspace_id, created_at FROM terminology
        """
    )
    connection.exec_driver_sql("DROP TABLE terminology")
    connection.exec_driver_sql("ALTER TABLE terminology_v2 RENAME TO terminology")
    _create_terminology_constraints(connection, "sqlite")


def _rebuild_terminology_pg(connection) -> None:
    """PG：drop 旧主键，加自增 id 主键与作用域唯一约束。

    加 id 列（旧表没有）→ 重建主键 → 唯一约束。正式部署后可换 Alembic。
    """
    with suppress(Exception):
        connection.exec_driver_sql("ALTER TABLE terminology DROP CONSTRAINT terminology_pkey")
    connection.exec_driver_sql("ALTER TABLE terminology ADD COLUMN id SERIAL")
    connection.exec_driver_sql("ALTER TABLE terminology ADD PRIMARY KEY (id)")
    _create_terminology_constraints(connection, "postgresql")


def _create_terminology_constraints(connection, dialect: str) -> None:
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_terminology_scope_term ON terminology(term, datasource_id, workspace_id)"
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_terminology_datasource_id ON terminology(datasource_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_terminology_term ON terminology(term)")


def upgrade_knowledge_schema(connection) -> None:
    """给旧版 sql_examples / terminology 补作用域列与索引；术语表按需重建。

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

    # 术语表主键仍是 term（旧部署）→ 重建为 id 主键 + 作用域唯一
    if _terminology_pk_is_term(inspector, tables):
        if dialect == "sqlite":
            _rebuild_terminology_sqlite(connection)
        else:
            _rebuild_terminology_pg(connection)

    for index, table, column in _KNOWLEDGE_INDEXES:
        if table in tables:
            connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {index} ON {table}({column})")
