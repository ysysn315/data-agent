"""SQL 知识库（示例/术语表）作用域列的幂等兼容迁移。

背景与 graph_migration.py 相同：项目仍以 ``Base.metadata.create_alL`` 起步、
没有 Alembic，``create_all`` 不会给已有表补新列。本轮给 sql_examples / terminology
加作用域列（datasource_id/workspace_id，示例表另有 source/meta），旧数据按默认值
归入演示作用域（datasource_id=NULL、workspace_id=0、source='manual'），行为不变。

术语表额外走一步重建：旧版 term 是主键（全局唯一），作用域化后唯一键改为
(scope_key, term)——同一术语可在不同作用域各自存在。scope_key 非空
（"datasource:N" / "workspace:N"，graph_triples 同款），规避 SQL 标准
"唯一索引中 NULL 互不相等"的漏洞。SQLite 不能删主键约束，参照 graph_migration
的临时表复制；PG 则 drop 旧主键再建唯一约束。主键换成自增 id。
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
    ("terminology", "scope_key", "VARCHAR(128) NOT NULL DEFAULT 'workspace:0'"),
]

_KNOWLEDGE_INDEXES: list[tuple[str, str, str]] = [
    ("ix_sql_examples_datasource_id", "sql_examples", "datasource_id"),
    ("ix_terminology_datasource_id", "terminology", "datasource_id"),
]


def terminology_scope_key(datasource_id: int | None, workspace_id: int) -> str:
    """作用域键：平台数据源按数据源，演示按 workspace（graph.scope.GraphScope 同款口径）。"""
    if datasource_id is not None:
        return f"datasource:{datasource_id}"
    return f"workspace:{workspace_id or 0}"


def _terminology_needs_rebuild(inspector, tables: list[str]) -> bool:
    """旧版 terminology 需要重建：term 是主键，或缺 scope_key 列（NULL 唯一漏洞版本）。"""
    if "terminology" not in tables:
        return False
    pk = inspector.get_pk_constraint("terminology").get("constrained_columns") or []
    if pk == ["term"]:
        return True
    columns = {c["name"] for c in inspector.get_columns("terminology")}
    return "scope_key" not in columns


def _rebuild_terminology_sqlite(connection) -> None:
    """SQLite：主键不能删，临时表复制（同 graph_migration 的做法）。

    兼容三种旧结构：term 主键 / 已有作用域列但无 scope_key / 完整新版（不会进来）。
    scope_key 从既有 datasource_id/workspace_id 推导，旧数据（NULL/0）归演示作用域。
    """
    has_ds = "datasource_id" in {c["name"] for c in inspect(connection).get_columns("terminology")}
    ds_sel = "datasource_id" if has_ds else "NULL"
    ws_sel = "workspace_id" if has_ds else "0"

    connection.exec_driver_sql(
        """
        CREATE TABLE terminology_v2 (
            id INTEGER NOT NULL PRIMARY KEY,
            term VARCHAR(256) NOT NULL,
            synonyms JSON NOT NULL,
            definition TEXT NOT NULL,
            sql_hint TEXT,
            scope_key VARCHAR(128) NOT NULL DEFAULT 'workspace:0',
            datasource_id INTEGER,
            workspace_id INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        f"""
        INSERT INTO terminology_v2 (term, synonyms, definition, sql_hint, datasource_id, workspace_id, created_at)
        SELECT term, synonyms, definition, sql_hint, {ds_sel}, {ws_sel}, created_at FROM terminology
        """
    )
    # scope_key 由迁移后的列推导（方言内都能跑的 CASE 表达式）
    connection.exec_driver_sql(
        """
        UPDATE terminology_v2
        SET scope_key = CASE WHEN datasource_id IS NOT NULL
            THEN 'datasource:' || datasource_id
            ELSE 'workspace:' || workspace_id END
        """
    )
    _dedup_terminology_by_scope(connection, "terminology_v2")
    connection.exec_driver_sql("DROP TABLE terminology")
    connection.exec_driver_sql("ALTER TABLE terminology_v2 RENAME TO terminology")
    _create_terminology_constraints(connection)


def _rebuild_terminology_pg(connection) -> None:
    """PG：drop 旧主键，加自增 id 主键与作用域唯一约束。正式部署后可换 Alembic。"""
    inspector = inspect(connection)
    columns = {c["name"] for c in inspector.get_columns("terminology")}
    if "id" not in columns:
        with suppress(Exception):
            connection.exec_driver_sql("ALTER TABLE terminology DROP CONSTRAINT terminology_pkey")
        connection.exec_driver_sql("ALTER TABLE terminology ADD COLUMN id SERIAL")
        connection.exec_driver_sql("ALTER TABLE terminology ADD PRIMARY KEY (id)")
    if "scope_key" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE terminology ADD COLUMN scope_key VARCHAR(128) NOT NULL DEFAULT 'workspace:0'"
        )
        connection.exec_driver_sql(
            "UPDATE terminology SET scope_key = CASE WHEN datasource_id IS NOT NULL "
            "THEN 'datasource:' || datasource_id ELSE 'workspace:' || workspace_id END"
        )
    _dedup_terminology_by_scope(connection, "terminology")
    _create_terminology_constraints(connection)


def _dedup_terminology_by_scope(connection, table: str) -> None:
    """建唯一索引前清理同 (scope_key, term) 重复行，保留 id 最大（最新写入）的一条。

    旧"NULL 唯一漏洞版"可能已产生演示作用域重复 term，直接建索引会 IntegrityError
    启动失败；保留最新一条并记日志，被合并掉的旧条目口径视为已被覆盖。
    """
    from loguru import logger

    removed = connection.exec_driver_sql(
        f"""
        DELETE FROM {table}
        WHERE id NOT IN (
            SELECT MAX(id) FROM {table} GROUP BY scope_key, term
        )
        """
    )
    if removed.rowcount:
        logger.warning(f"术语表作用域迁移清理重复词条 {removed.rowcount} 条（保留最新一条）")


def _create_terminology_constraints(connection) -> None:
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_terminology_scope_term ON terminology(scope_key, term)"
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
        # 术语表的 scope_key 走重建路径补齐（含数据回填），不在 ADD COLUMN 里做
        if table == "terminology" and column == "scope_key":
            continue
        if column == "meta" and dialect != "sqlite":
            # PG 的 JSON 列默认值需要显式 ::json 转型
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} JSON NOT NULL DEFAULT '{{}}'::json")
        else:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # 术语表是旧结构（term 主键 / 缺 scope_key）→ 重建为 id 主键 + scope_key 唯一
    if _terminology_needs_rebuild(inspector, tables):
        if dialect == "sqlite":
            _rebuild_terminology_sqlite(connection)
        else:
            _rebuild_terminology_pg(connection)

    for index, table, column in _KNOWLEDGE_INDEXES:
        if table in tables:
            connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {index} ON {table}({column})")
