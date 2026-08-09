"""Agent 工具使用的同步数据源运行时。"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from app.agents.tools.sql_guard import validate_sql
from app.datasources.connectors import ConnectorRegistry
from app.datasources.m_schema import render_m_schema, schema_map
from app.datasources.models import (
    ConnectionSpec,
    DataSourceConnectionError,
    DataSourceKind,
    DataSourceNotFoundError,
)
from app.datasources.repository import DataSourceRepository
from app.datasources.security import CredentialCipher


class DataSourceRuntime:
    def __init__(
        self,
        *,
        repository: DataSourceRepository,
        cipher: CredentialCipher,
        connectors: ConnectorRegistry,
        runner: Callable[[Coroutine[Any, Any, Any]], Any],
    ):
        self._repo = repository
        self._cipher = cipher
        self._connectors = connectors
        self._run = runner

    def _source_and_catalog(self, datasource_id: int, workspace_id: int) -> tuple[dict, dict]:
        source = self._run(self._repo.get_internal(datasource_id, workspace_id))
        catalog = self._run(self._repo.get_catalog(datasource_id, workspace_id))
        if source is None or catalog is None:
            raise DataSourceNotFoundError("数据源不存在或无权访问")
        if source.get("status") != "ready":
            raise DataSourceConnectionError("数据源上次结构同步失败，请由管理员重新同步后再查询")
        return source, catalog

    def get_m_schema(self, datasource_id: int, workspace_id: int) -> str:
        _source, catalog = self._source_and_catalog(datasource_id, workspace_id)
        return render_m_schema(catalog, include_pending=False)

    def execute_sql(self, datasource_id: int, workspace_id: int, sql: str, limit: int) -> dict:
        source, catalog = self._source_and_catalog(datasource_id, workspace_id)
        kind = DataSourceKind(source["kind"])
        spec = ConnectionSpec(
            kind=kind,
            config=dict(source["connection_config"]),
            credentials=self._cipher.decrypt(source.get("encrypted_credentials")),
        )
        schema = schema_map(catalog)
        if kind == DataSourceKind.SQLITE:
            schema["sqlite_master"] = ["type", "name", "tbl_name", "rootpage", "sql"]
        dialect = self._connectors.dialect(kind)
        guard = validate_sql(sql, schema=schema, default_limit=1000, dialect=dialect)
        if not guard.ok:
            raise ValueError(guard.error or "SQL 校验失败")
        assert guard.fixed_sql is not None
        return self._connectors.execute(spec, guard.fixed_sql, limit)
