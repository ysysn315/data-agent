"""图谱平台化的幂等兼容迁移。

项目当前仍以 ``Base.metadata.create_all`` 起步，没有 Alembic。新增作用域字段时，
``create_all`` 不会修改已有 ``graph_triples``，因此在建表前对旧版表做一次小而
明确的迁移：旧数据归入演示 ``workspace:0``，原始三元组和来源全部保留。
"""

from __future__ import annotations

from contextlib import suppress

from sqlalchemy import inspect


def upgrade_graph_schema(connection) -> None:
    """把旧版全局 graph_triples 升级为带 scope 的表。

    测试和新数据库没有旧表时是 no-op。SQLite 不能删除旧唯一约束，故采用临时
    表复制；PG/其它数据库先用 ADD COLUMN 并删除旧约束，后续正式部署可迁移到
    Alembic。
    """

    inspector = inspect(connection)
    if "graph_triples" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("graph_triples")}
    if "scope_key" in columns:
        return

    dialect = connection.dialect.name
    if dialect == "sqlite":
        connection.exec_driver_sql(
            """
            CREATE TABLE graph_triples_v2 (
                id INTEGER NOT NULL PRIMARY KEY,
                subject VARCHAR(256) NOT NULL,
                predicate VARCHAR(256) NOT NULL,
                object VARCHAR(256) NOT NULL,
                scope_key VARCHAR(128) NOT NULL DEFAULT 'workspace:0',
                workspace_id INTEGER NOT NULL DEFAULT 0,
                datasource_id INTEGER,
                subject_entity_id INTEGER,
                object_entity_id INTEGER,
                source VARCHAR(64) NOT NULL DEFAULT 'manual',
                source_ref VARCHAR(512),
                provenance JSON NOT NULL DEFAULT '{}',
                created_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO graph_triples_v2
                (id, subject, predicate, object, source, created_at)
            SELECT id, subject, predicate, object, source, created_at
            FROM graph_triples
            """
        )
        connection.exec_driver_sql("DROP TABLE graph_triples")
        connection.exec_driver_sql("ALTER TABLE graph_triples_v2 RENAME TO graph_triples")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_graph_triples_scope_spo "
            "ON graph_triples(scope_key, subject, predicate, object)"
        )
        connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_graph_triples_scope_key ON graph_triples(scope_key)")
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_graph_triples_workspace_id ON graph_triples(workspace_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_graph_triples_datasource_id ON graph_triples(datasource_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_graph_triples_subject_entity_id ON graph_triples(subject_entity_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_graph_triples_object_entity_id ON graph_triples(object_entity_id)"
        )
        return

    # PostgreSQL and other production databases: preserve rows and remove the old
    # global constraint before adding the scoped one. Existing deployments should
    # eventually move this block to versioned Alembic migrations.
    connection.exec_driver_sql(
        "ALTER TABLE graph_triples ADD COLUMN scope_key VARCHAR(128) NOT NULL DEFAULT 'workspace:0'"
    )
    connection.exec_driver_sql("ALTER TABLE graph_triples ADD COLUMN workspace_id INTEGER NOT NULL DEFAULT 0")
    connection.exec_driver_sql("ALTER TABLE graph_triples ADD COLUMN datasource_id INTEGER")
    connection.exec_driver_sql("ALTER TABLE graph_triples ADD COLUMN subject_entity_id INTEGER")
    connection.exec_driver_sql("ALTER TABLE graph_triples ADD COLUMN object_entity_id INTEGER")
    connection.exec_driver_sql("ALTER TABLE graph_triples ADD COLUMN source_ref VARCHAR(512)")
    connection.exec_driver_sql("ALTER TABLE graph_triples ADD COLUMN provenance JSON NOT NULL DEFAULT '{}'::json")
    with suppress(Exception):
        connection.exec_driver_sql("ALTER TABLE graph_triples DROP CONSTRAINT uq_graph_triples_spo")
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_graph_triples_scope_spo "
        "ON graph_triples(scope_key, subject, predicate, object)"
    )
