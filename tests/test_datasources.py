"""数据源接入、AI 语义草稿、人工审核与 Agent 工具闭环测试。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager, nullcontext
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.agents.tools.schema_tool import create_schema_search_tool
from app.agents.tools.sql_tool import create_execute_sql_tool
from app.api.routes_datasources import get_datasource_service
from app.datasources.connectors import ConnectorRegistry, SQLAlchemyConnector
from app.datasources.context import current_selection, use_datasource
from app.datasources.models import (
    ColumnSnapshot,
    ConnectionSpec,
    DataSourceConfigError,
    DataSourceConnectionError,
    DataSourceKind,
    ReviewStatus,
    SchemaSnapshot,
    TableSnapshot,
)
from app.datasources.repository import DataSourceRepository
from app.datasources.runtime import DataSourceRuntime
from app.datasources.security import CredentialCipher, resolve_sqlite_path, sanitize_error
from app.datasources.service import DataSourceService
from app.db import create_engine_and_sessionmaker, init_db, run_sync
from app.db.models import DataSourceModel
from app.main import app
from app.schemas.datasource import DataSourceCreate, SemanticDraftRequest
from app.services.chat_service import ChatService


def _make_business_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            customer_name TEXT NOT NULL
        );
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
        );
        INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob');
        INSERT INTO orders VALUES (10, 1, 88.5), (11, 2, 21.0);
        """
    )
    conn.commit()
    conn.close()


class _FakeSemanticLLM:
    async def ainvoke(self, messages):
        physical = json.loads(messages[-1].content)
        table_name = physical["table"]
        payload = {
            "table_comment": f"{table_name} 业务表",
            "columns": [
                {
                    "name": column["name"],
                    "comment": f"{column['name']} 业务含义",
                    "synonyms": [f"{column['name']}别名"],
                }
                for column in physical["columns"]
            ],
        }
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))


@pytest.fixture
async def datasource_env(tmp_path):
    engine, sessionmaker = create_engine_and_sessionmaker(
        f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}"
    )
    await init_db(engine)
    repo = DataSourceRepository(sessionmaker)
    cipher = CredentialCipher(Fernet.generate_key().decode("ascii"))
    connectors = ConnectorRegistry()
    service = DataSourceService(
        repository=repo,
        cipher=cipher,
        connectors=connectors,
        sqlite_root=str(tmp_path),
        llm_provider=lambda: _FakeSemanticLLM(),
    )
    yield {
        "engine": engine,
        "sessionmaker": sessionmaker,
        "repo": repo,
        "cipher": cipher,
        "connectors": connectors,
        "service": service,
        "root": tmp_path,
    }
    await engine.dispose()


async def _create_sqlite_source(env, name: str = "sales") -> tuple[dict, Path]:
    path = env["root"] / f"{name}.db"
    _make_business_db(path)
    source = await env["service"].create(
        {"name": name, "kind": "sqlite", "path": path.name},
        workspace_id=None,
    )
    return source, path


def test_credential_cipher_and_error_redaction():
    key = Fernet.generate_key().decode("ascii")
    cipher = CredentialCipher(key)
    token = cipher.encrypt({"username": "analyst", "password": "super-secret"})
    assert token and "super-secret" not in token
    assert cipher.decrypt(token) == {"username": "analyst", "password": "super-secret"}
    assert "super-secret" not in sanitize_error(
        RuntimeError("postgresql://analyst:super-secret@db.local/sales failed"),
        {"username": "analyst", "password": "super-secret"},
    )

    with pytest.raises(DataSourceConfigError, match="DATASOURCE_SECRET_KEY"):
        CredentialCipher("").encrypt({"password": "x"})


def test_sqlite_path_must_stay_under_allowed_root(tmp_path):
    inside = tmp_path / "inside.db"
    inside.touch()
    assert resolve_sqlite_path("inside.db", str(tmp_path)) == inside.resolve()

    outside = tmp_path.parent / "outside.db"
    outside.touch(exist_ok=True)
    with pytest.raises(DataSourceConfigError, match="允许目录"):
        resolve_sqlite_path(str(outside), str(tmp_path))


def test_semantic_draft_request_caps_batch_size():
    with pytest.raises(ValidationError):
        SemanticDraftRequest(table_ids=list(range(101)))


def test_remote_payload_requires_tls_by_default_and_mysql_has_one_schema():
    payload = DataSourceCreate(
        name="warehouse",
        kind="postgresql",
        host="db.internal",
        database="analytics",
        username="reader",
        password="secret",
    )
    assert payload.ssl_mode == "require"

    with pytest.raises(ValidationError, match="database 即 schema"):
        DataSourceCreate(
            name="mysql",
            kind="mysql",
            host="db.internal",
            database="analytics",
            schema="other",
            username="reader",
            password="secret",
        )


def test_remote_sql_errors_redact_query_and_credentials(monkeypatch):
    sql = "SELECT secret_value FROM reports"
    spec = ConnectionSpec(
        kind=DataSourceKind.POSTGRESQL,
        config={"host": "db.internal", "database": "analytics", "schema": ""},
        credentials={"username": "reader", "password": "top-secret"},
    )

    class _FailingConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def begin(self):
            return nullcontext()

        def exec_driver_sql(self, statement):
            if statement == sql:
                raise SQLAlchemyError(f"query failed: {sql}; password=top-secret")
            return None

    class _FakeEngine:
        def connect(self):
            return _FailingConnection()

    @contextmanager
    def _fake_engine(_spec):
        yield _FakeEngine()

    connector = SQLAlchemyConnector(DataSourceKind.POSTGRESQL)
    monkeypatch.setattr(connector, "_engine", _fake_engine)
    with pytest.raises(DataSourceConnectionError) as error:
        connector.execute(spec, sql, 100)
    assert sql not in str(error.value)
    assert "top-secret" not in str(error.value)


async def test_sqlite_onboarding_auto_discovers_schema(datasource_env):
    source, _path = await _create_sqlite_source(datasource_env)
    assert source["status"] == "ready"
    assert source["connection_summary"] == "sales.db"
    assert "connection_config" not in source
    assert "encrypted_credentials" not in source

    catalog = await datasource_env["service"].get_catalog(source["id"], None)
    assert [table["table_name"] for table in catalog["tables"]] == ["customers", "orders"]
    orders = next(table for table in catalog["tables"] if table["table_name"] == "orders")
    customer_id = next(column for column in orders["columns"] if column["column_name"] == "customer_id")
    assert customer_id["references"] == {"table": "customers", "column": "customer_id"}
    assert all(table["review_status"] == "pending" for table in catalog["tables"])


async def test_ai_draft_is_not_active_until_review(datasource_env):
    source, _path = await _create_sqlite_source(datasource_env)
    result = await datasource_env["service"].generate_semantic_drafts(source["id"], None)
    assert result == {"datasource_id": source["id"], "drafted_table_count": 2, "status": "pending"}

    production_schema = await datasource_env["service"].get_m_schema(source["id"], None)
    review_preview = await datasource_env["service"].get_m_schema(
        source["id"], None, include_pending=True
    )
    assert "orders 业务表" not in production_schema
    assert "orders 业务表" in review_preview

    catalog = await datasource_env["service"].get_catalog(source["id"], None)
    orders = next(table for table in catalog["tables"] if table["table_name"] == "orders")
    columns = [
        {
            "name": column["column_name"],
            "comment": column["ai_comment"],
            "synonyms": column["ai_synonyms"],
        }
        for column in orders["columns"]
    ]
    reviewed = await datasource_env["service"].review_table(
        datasource_id=source["id"],
        workspace_id=None,
        table_id=orders["id"],
        decision=ReviewStatus.APPROVED,
        table_comment="已审核订单事实表",
        columns=columns,
    )
    assert reviewed["review_status"] == "approved"

    production_schema = await datasource_env["service"].get_m_schema(source["id"], None)
    assert "已审核订单事实表" in production_schema
    assert "order_id 业务含义" in production_schema
    assert "customers 业务表" not in production_schema


async def test_schema_drift_keeps_unchanged_columns_but_reopens_table_review(datasource_env):
    source, path = await _create_sqlite_source(datasource_env)
    await datasource_env["service"].generate_semantic_drafts(source["id"], None)
    catalog = await datasource_env["service"].get_catalog(source["id"], None)
    orders = next(table for table in catalog["tables"] if table["table_name"] == "orders")
    await datasource_env["service"].review_table(
        datasource_id=source["id"],
        workspace_id=None,
        table_id=orders["id"],
        decision=ReviewStatus.APPROVED,
        table_comment=None,
        columns=[
            {
                "name": column["column_name"],
                "comment": None,
                "synonyms": column["ai_synonyms"],
            }
            for column in orders["columns"]
        ],
    )

    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE orders ADD COLUMN channel TEXT")
    conn.commit()
    conn.close()
    await datasource_env["service"].sync(source["id"], None)

    updated = await datasource_env["service"].get_catalog(source["id"], None)
    orders = next(table for table in updated["tables"] if table["table_name"] == "orders")
    assert orders["review_status"] == "pending"
    status_by_column = {column["column_name"]: column["review_status"] for column in orders["columns"]}
    assert status_by_column["order_id"] == "approved"
    assert status_by_column["channel"] == "pending"


class _StaticConnectorRegistry:
    def __init__(self, snapshot: SchemaSnapshot):
        self.snapshot = snapshot
        self.last_spec = None

    def scan(self, spec):
        self.last_spec = spec
        return self.snapshot


async def test_remote_credentials_are_encrypted_and_never_returned(datasource_env):
    snapshot = SchemaSnapshot(
        tables=(
            TableSnapshot(
                schema_name="public",
                name="events",
                table_type="table",
                physical_comment="",
                columns=(
                    ColumnSnapshot("event_id", "BIGINT", 0, primary_key=True),
                ),
            ),
        )
    )
    connectors = _StaticConnectorRegistry(snapshot)
    disabled_service = DataSourceService(
        repository=datasource_env["repo"],
        cipher=datasource_env["cipher"],
        connectors=connectors,
        sqlite_root=str(datasource_env["root"]),
    )
    with pytest.raises(DataSourceConfigError, match="DATASOURCE_REMOTE_ENABLED"):
        await disabled_service.create(
            {
                "name": "disabled",
                "kind": "postgresql",
                "host": "db.internal",
                "database": "analytics",
                "username": "reader",
                "password": "secret",
            },
            workspace_id=7,
        )

    service = DataSourceService(
        repository=datasource_env["repo"],
        cipher=datasource_env["cipher"],
        connectors=connectors,
        sqlite_root=str(datasource_env["root"]),
        remote_enabled=True,
    )
    result = await service.create(
        {
            "name": "warehouse",
            "kind": "postgresql",
            "host": "db.internal",
            "port": 5432,
            "database": "analytics",
            "schema": "public",
            "username": "reader",
            "password": "do-not-store-plaintext",
            "ssl_mode": "require",
        },
        workspace_id=7,
    )
    assert "reader" not in json.dumps(result)
    assert "do-not-store-plaintext" not in json.dumps(result)
    assert connectors.last_spec.credentials["password"] == "do-not-store-plaintext"

    async with datasource_env["sessionmaker"]() as session:
        row = (
            await session.execute(select(DataSourceModel).where(DataSourceModel.id == result["id"]))
        ).scalar_one()
        assert "do-not-store-plaintext" not in row.encrypted_credentials
        assert "reader" not in json.dumps(row.connection_config)
        assert datasource_env["cipher"].decrypt(row.encrypted_credentials) == {
            "username": "reader",
            "password": "do-not-store-plaintext",
        }


async def test_workspace_isolation(datasource_env):
    source, path = await _create_sqlite_source(datasource_env)
    assert await datasource_env["repo"].get_source(source["id"], workspace_id=999) is None
    with pytest.raises(Exception, match="数据源不存在"):
        await datasource_env["service"].get_catalog(source["id"], workspace_id=999)
    assert path.exists()


async def test_selected_datasource_drives_schema_and_sql_tools(datasource_env):
    source, _path = await _create_sqlite_source(datasource_env)
    await datasource_env["service"].generate_semantic_drafts(source["id"], None)
    catalog = await datasource_env["service"].get_catalog(source["id"], None)
    orders = next(table for table in catalog["tables"] if table["table_name"] == "orders")
    await datasource_env["service"].review_table(
        datasource_id=source["id"],
        workspace_id=None,
        table_id=orders["id"],
        decision=ReviewStatus.APPROVED,
        table_comment="订单事实表",
        columns=[
            {
                "name": column["column_name"],
                "comment": None,
                "synonyms": column["ai_synonyms"],
            }
            for column in orders["columns"]
        ],
    )

    runtime = DataSourceRuntime(
        repository=datasource_env["repo"],
        cipher=datasource_env["cipher"],
        connectors=datasource_env["connectors"],
        runner=run_sync,
    )
    schema_tool = create_schema_search_tool("/definitely/missing.db", datasource_runtime=runtime)
    sql_tool = create_execute_sql_tool("/definitely/missing.db", datasource_runtime=runtime)
    with use_datasource(source["id"], 0):
        schema_text = schema_tool.invoke({"question": "订单金额"})
        sql_result = json.loads(sql_tool.invoke({"sql": "SELECT SUM(amount) AS total FROM orders"}))

    assert "订单事实表" in schema_text
    assert sql_result["columns"] == ["total"]
    assert sql_result["rows"] == [[109.5]]

    await datasource_env["repo"].mark_error(source["id"], 0, "sync failed")
    with pytest.raises(DataSourceConnectionError, match="重新同步"):
        runtime.get_m_schema(source["id"], 0)


async def test_datasource_api_end_to_end(datasource_env):
    source_db = datasource_env["root"] / "api.db"
    _make_business_db(source_db)
    app.dependency_overrides[get_datasource_service] = lambda: datasource_env["service"]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/datasources",
                json={"name": "api-source", "kind": "sqlite", "path": "api.db"},
            )
            assert created.status_code == 201
            source_id = created.json()["id"]
            assert (await client.get("/api/datasources")).status_code == 200

            drafted = await client.post(
                f"/api/datasources/{source_id}/semantic-draft",
                json={},
            )
            assert drafted.status_code == 200
            metadata = (await client.get(f"/api/datasources/{source_id}/metadata")).json()
            orders = next(table for table in metadata["tables"] if table["table_name"] == "orders")
            review = await client.put(
                f"/api/datasources/{source_id}/metadata/{orders['id']}/review",
                json={
                    "decision": "approved",
                    "table_comment": "API 审核订单表",
                    "columns": [
                        {
                            "name": column["column_name"],
                            "comment": column["ai_comment"],
                            "synonyms": column["ai_synonyms"],
                        }
                        for column in orders["columns"]
                    ],
                },
            )
            assert review.status_code == 200
            m_schema = (await client.get(f"/api/datasources/{source_id}/m-schema")).json()["m_schema"]
            assert "API 审核订单表" in m_schema
    finally:
        app.dependency_overrides.clear()


class _ContextReadingAgent:
    def __init__(self):
        self.seen = None

    async def chat(self, question, history=None, summary=""):
        self.seen = await asyncio.to_thread(current_selection)
        return "ok"


class _MemorySessionStore:
    def __init__(self):
        self.messages = []

    def get_history(self, _session_id):
        return []

    def get_summary(self, _session_id):
        return ""

    def add_message(self, session_id, role, content):
        self.messages.append((session_id, role, content))


async def test_chat_service_propagates_and_resets_datasource_context():
    agent = _ContextReadingAgent()
    service = ChatService(agent, _MemorySessionStore())
    result = await service.chat("s1", "question", datasource_id=12, workspace_id=34)
    assert result["answer"] == "ok"
    assert agent.seen.datasource_id == 12
    assert agent.seen.workspace_id == 34
    assert current_selection() is None
