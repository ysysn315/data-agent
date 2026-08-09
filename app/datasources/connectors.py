"""SQLite/PostgreSQL/MySQL 只读连接器。

扫描与执行均为同步阻塞操作，上层服务在 FastAPI 路径用 asyncio.to_thread 调度；
Agent 工具本身由 LangChain 在线程池中调用，可直接使用同步接口。
"""

from __future__ import annotations

import sqlite3
import ssl
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import URL, create_engine, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from app.datasources.models import (
    ColumnSnapshot,
    ConnectionSpec,
    DataSourceConfigError,
    DataSourceConnectionError,
    DataSourceKind,
    SchemaSnapshot,
    TableSnapshot,
)
from app.datasources.security import sanitize_error

CONNECT_TIMEOUT_SECONDS = 5
QUERY_TIMEOUT_SECONDS = 10


def _quote_sqlite_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class SQLiteConnector:
    dialect = "sqlite"

    @staticmethod
    def _path(spec: ConnectionSpec) -> Path:
        path = Path(str(spec.config.get("path") or ""))
        if not path.exists() or not path.is_file():
            raise DataSourceConfigError("SQLite 数据源文件不可用，请由管理员检查配置并重新同步")
        return path

    @contextmanager
    def _connect(self, spec: ConnectionSpec) -> Iterator[sqlite3.Connection]:
        path = self._path(spec)
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=CONNECT_TIMEOUT_SECONDS)
        try:
            yield conn
        finally:
            conn.close()

    def scan(self, spec: ConnectionSpec) -> SchemaSnapshot:
        try:
            with self._connect(spec) as conn:
                raw_tables = conn.execute(
                    "SELECT name, type FROM sqlite_master "
                    "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                tables: list[TableSnapshot] = []
                for table_name, table_type in raw_tables:
                    quoted = _quote_sqlite_identifier(table_name)
                    column_rows = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
                    fk_rows = conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
                    references = {
                        row[3]: {"table": row[2], "column": row[4]}
                        for row in fk_rows
                        if row[3] and row[2] and row[4]
                    }
                    columns = tuple(
                        ColumnSnapshot(
                            name=row[1],
                            data_type=(row[2] or "UNKNOWN").upper(),
                            ordinal_position=int(row[0]),
                            nullable=not bool(row[3]),
                            primary_key=bool(row[5]),
                            references=references.get(row[1], {}),
                        )
                        for row in column_rows
                    )
                    if columns:
                        tables.append(
                            TableSnapshot(
                                schema_name="main",
                                name=table_name,
                                table_type=table_type,
                                physical_comment="",
                                columns=columns,
                            )
                        )
            return SchemaSnapshot(tables=tuple(tables))
        except (sqlite3.Error, OSError, DataSourceConfigError) as exc:
            if isinstance(exc, DataSourceConfigError):
                raise
            raise DataSourceConnectionError(sanitize_error(exc)) from exc

    def execute(self, spec: ConnectionSpec, sql: str, limit: int) -> dict:
        started = time.monotonic()
        try:
            with self._connect(spec) as conn:
                conn.set_progress_handler(
                    lambda: 1 if time.monotonic() - started > QUERY_TIMEOUT_SECONDS else 0,
                    1000,
                )
                cursor = conn.execute(sql)
                columns = [item[0] for item in cursor.description] if cursor.description else []
                rows = cursor.fetchmany(limit)
            return {"columns": columns, "rows": [list(row) for row in rows], "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise DataSourceConnectionError(sanitize_error(exc)) from exc


class SQLAlchemyConnector:
    """PostgreSQL/MySQL 元数据扫描与只读查询连接器。"""

    _DRIVERS = {
        DataSourceKind.POSTGRESQL: "postgresql+psycopg",
        DataSourceKind.MYSQL: "mysql+pymysql",
    }
    _DIALECTS = {
        DataSourceKind.POSTGRESQL: "postgres",
        DataSourceKind.MYSQL: "mysql",
    }

    def __init__(self, kind: DataSourceKind):
        if kind not in self._DRIVERS:
            raise ValueError(f"不支持的 SQLAlchemy 数据源类型: {kind}")
        self.kind = kind
        self.dialect = self._DIALECTS[kind]

    def _url(self, spec: ConnectionSpec) -> URL:
        config = spec.config
        credentials = spec.credentials
        query: dict[str, str] = {}
        ssl_mode = str(config.get("ssl_mode") or "").strip()
        if self.kind == DataSourceKind.POSTGRESQL:
            query["connect_timeout"] = str(CONNECT_TIMEOUT_SECONDS)
            if ssl_mode:
                query["sslmode"] = ssl_mode
        elif self.kind == DataSourceKind.MYSQL:
            query["connect_timeout"] = str(CONNECT_TIMEOUT_SECONDS)
            query["read_timeout"] = str(QUERY_TIMEOUT_SECONDS)

        return URL.create(
            drivername=self._DRIVERS[self.kind],
            username=credentials.get("username") or None,
            password=credentials.get("password") or None,
            host=str(config.get("host") or ""),
            port=int(config["port"]) if config.get("port") else None,
            database=str(config.get("database") or ""),
            query=query,
        )

    @contextmanager
    def _engine(self, spec: ConnectionSpec) -> Iterator[Engine]:
        connect_args: dict = {}
        if self.kind == DataSourceKind.MYSQL:
            ssl_mode = str(spec.config.get("ssl_mode") or "")
            if ssl_mode == "require":
                connect_args["ssl"] = ssl.create_default_context()
            elif ssl_mode == "disable":
                connect_args["ssl_disabled"] = True
        try:
            engine = create_engine(
                self._url(spec),
                poolclass=NullPool,
                future=True,
                connect_args=connect_args,
            )
        except (SQLAlchemyError, ModuleNotFoundError) as exc:
            raise DataSourceConnectionError(sanitize_error(exc, spec.credentials)) from exc

        # yield 之后的 SQL 异常由 scan/execute 各自处理。特别是 execute 需要把原始
        # SQL 也作为敏感值脱敏，不能在这里提前吞掉后丢失该上下文。
        try:
            yield engine
        finally:
            engine.dispose()

    @staticmethod
    def _safe_table_comment(inspector, table_name: str, schema_name: str | None) -> str:
        try:
            result = inspector.get_table_comment(table_name, schema=schema_name)
            return str((result or {}).get("text") or "")
        except (NotImplementedError, SQLAlchemyError):
            return ""

    def scan(self, spec: ConnectionSpec) -> SchemaSnapshot:
        schema_config = str(spec.config.get("schema") or "").strip() or None
        try:
            with self._engine(spec) as engine, engine.connect() as conn:
                inspector = inspect(conn)
                schema_name = schema_config or inspector.default_schema_name
                table_names = [(name, "table") for name in inspector.get_table_names(schema=schema_name)]
                table_names.extend((name, "view") for name in inspector.get_view_names(schema=schema_name))

                tables: list[TableSnapshot] = []
                for table_name, table_type in sorted(table_names):
                    raw_columns = inspector.get_columns(table_name, schema=schema_name)
                    pk_columns = set(
                        (inspector.get_pk_constraint(table_name, schema=schema_name) or {}).get(
                            "constrained_columns"
                        )
                        or []
                    )
                    fk_by_column: dict[str, dict[str, str]] = {}
                    for fk in inspector.get_foreign_keys(table_name, schema=schema_name):
                        constrained = fk.get("constrained_columns") or []
                        referred = fk.get("referred_columns") or []
                        for local, remote in zip(constrained, referred):
                            fk_by_column[local] = {
                                "schema": str(fk.get("referred_schema") or schema_name or ""),
                                "table": str(fk.get("referred_table") or ""),
                                "column": str(remote or ""),
                            }

                    columns = tuple(
                        ColumnSnapshot(
                            name=str(column["name"]),
                            data_type=str(column.get("type") or "UNKNOWN").upper(),
                            ordinal_position=index,
                            nullable=bool(column.get("nullable", True)),
                            primary_key=str(column["name"]) in pk_columns,
                            physical_comment=str(column.get("comment") or ""),
                            references=fk_by_column.get(str(column["name"]), {}),
                        )
                        for index, column in enumerate(raw_columns)
                    )
                    if columns:
                        tables.append(
                            TableSnapshot(
                                schema_name=str(schema_name or ""),
                                name=table_name,
                                table_type=table_type,
                                physical_comment=self._safe_table_comment(inspector, table_name, schema_name),
                                columns=columns,
                            )
                        )
            return SchemaSnapshot(tables=tuple(tables))
        except DataSourceConnectionError:
            raise
        except (SQLAlchemyError, ModuleNotFoundError) as exc:
            raise DataSourceConnectionError(sanitize_error(exc, spec.credentials)) from exc

    def _set_readonly(self, conn: Connection, spec: ConnectionSpec) -> None:
        if self.kind == DataSourceKind.POSTGRESQL:
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {QUERY_TIMEOUT_SECONDS * 1000}")
            schema_name = str(spec.config.get("schema") or "").strip()
            if schema_name:
                quoted = conn.dialect.identifier_preparer.quote_schema(schema_name)
                conn.exec_driver_sql(f"SET LOCAL search_path TO {quoted}")

    def _prepare_session(self, conn: Connection) -> None:
        if self.kind != DataSourceKind.MYSQL:
            return
        # MySQL 的 SET TRANSACTION 必须发生在目标事务开始前。NullPool 保证该只读
        # session 不会回收到其它请求；数据库账号本身仍必须只读，作为权限层兜底。
        conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME={QUERY_TIMEOUT_SECONDS * 1000}")
        conn.commit()

    def execute(self, spec: ConnectionSpec, sql: str, limit: int) -> dict:
        try:
            with self._engine(spec) as engine, engine.connect() as conn:
                self._prepare_session(conn)
                with conn.begin():
                    self._set_readonly(conn, spec)
                    result = conn.exec_driver_sql(sql)
                    rows = result.fetchmany(limit)
                    columns = list(result.keys())
            return {"columns": columns, "rows": [list(row) for row in rows], "row_count": len(rows)}
        except DataSourceConnectionError:
            raise
        except SQLAlchemyError as exc:
            sensitive = {**spec.credentials, "sql": sql}
            raise DataSourceConnectionError(sanitize_error(exc, sensitive)) from exc


class ConnectorRegistry:
    def __init__(self):
        self._connectors = {
            DataSourceKind.SQLITE: SQLiteConnector(),
            DataSourceKind.POSTGRESQL: SQLAlchemyConnector(DataSourceKind.POSTGRESQL),
            DataSourceKind.MYSQL: SQLAlchemyConnector(DataSourceKind.MYSQL),
        }

    def get(self, kind: DataSourceKind):
        try:
            return self._connectors[kind]
        except KeyError as exc:
            raise DataSourceConfigError(f"不支持的数据源类型: {kind}") from exc

    def scan(self, spec: ConnectionSpec) -> SchemaSnapshot:
        return self.get(spec.kind).scan(spec)

    def execute(self, spec: ConnectionSpec, sql: str, limit: int) -> dict:
        return self.get(spec.kind).execute(spec, sql, limit)

    def dialect(self, kind: DataSourceKind) -> str:
        return self.get(kind).dialect
