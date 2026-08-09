"""数据源与语义元数据的异步仓储。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.datasources.models import (
    ColumnSnapshot,
    DataSourceKind,
    DataSourceNotFoundError,
    ReviewStatus,
    SchemaSnapshot,
    TableDraft,
    TableSnapshot,
)
from app.db.models import DataSourceColumnModel, DataSourceModel, DataSourceTableModel


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _column_signature(column: ColumnSnapshot) -> str:
    return _stable_hash(
        {
            "name": column.name,
            "data_type": column.data_type,
            "ordinal_position": column.ordinal_position,
            "nullable": column.nullable,
            "primary_key": column.primary_key,
            "physical_comment": column.physical_comment,
            "references": column.references,
        }
    )


def _table_signature(table: TableSnapshot) -> str:
    return _stable_hash(
        {
            "schema_name": table.schema_name,
            "name": table.name,
            "table_type": table.table_type,
            "physical_comment": table.physical_comment,
            "columns": [_column_signature(column) for column in table.columns],
        }
    )


def _snapshot_hash(snapshot: SchemaSnapshot) -> str:
    return _stable_hash([_table_signature(table) for table in snapshot.tables])


class DataSourceRepository:
    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    @staticmethod
    def _connection_summary(row: DataSourceModel) -> str:
        config = dict(row.connection_config or {})
        if row.kind == DataSourceKind.SQLITE.value:
            return str(config.get("display_path") or config.get("path") or "")
        host = str(config.get("host") or "")
        port = f":{config['port']}" if config.get("port") else ""
        database = str(config.get("database") or "")
        schema = f"/{config['schema']}" if config.get("schema") else ""
        return f"{host}{port}/{database}{schema}"

    @classmethod
    def _source_to_public(cls, row: DataSourceModel) -> dict:
        return {
            "id": row.id,
            "workspace_id": row.workspace_id,
            "name": row.name,
            "kind": row.kind,
            "connection_summary": cls._connection_summary(row),
            "status": row.status,
            "last_error": row.last_error,
            "schema_hash": row.schema_hash,
            "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @classmethod
    def _source_to_internal(cls, row: DataSourceModel) -> dict:
        result = cls._source_to_public(row)
        result["connection_config"] = dict(row.connection_config or {})
        result["encrypted_credentials"] = row.encrypted_credentials
        return result

    @staticmethod
    async def _insert_snapshot(session, datasource_id: int, snapshot: SchemaSnapshot) -> None:
        for table in snapshot.tables:
            table_row = DataSourceTableModel(
                datasource_id=datasource_id,
                schema_name=table.schema_name,
                table_name=table.name,
                table_type=table.table_type,
                physical_comment=table.physical_comment,
                physical_signature=_table_signature(table),
                review_status=ReviewStatus.PENDING.value,
            )
            session.add(table_row)
            await session.flush()
            for column in table.columns:
                session.add(
                    DataSourceColumnModel(
                        table_id=table_row.id,
                        column_name=column.name,
                        data_type=column.data_type,
                        ordinal_position=column.ordinal_position,
                        nullable=column.nullable,
                        primary_key=column.primary_key,
                        physical_comment=column.physical_comment,
                        references=dict(column.references),
                        physical_signature=_column_signature(column),
                        review_status=ReviewStatus.PENDING.value,
                    )
                )

    async def create_with_snapshot(
        self,
        *,
        workspace_id: int,
        name: str,
        kind: DataSourceKind,
        connection_config: dict,
        encrypted_credentials: str | None,
        snapshot: SchemaSnapshot,
    ) -> dict:
        now = datetime.utcnow()
        async with self._sm() as session:
            row = DataSourceModel(
                workspace_id=workspace_id,
                name=name,
                kind=kind.value,
                connection_config=dict(connection_config),
                encrypted_credentials=encrypted_credentials,
                status="ready",
                last_error=None,
                schema_hash=_snapshot_hash(snapshot),
                last_synced_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                await session.flush()
                await self._insert_snapshot(session, row.id, snapshot)
                await session.commit()
                await session.refresh(row)
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError(f"当前工作空间已存在同名数据源: {name}") from exc
            return self._source_to_public(row)

    async def list_sources(self, workspace_id: int) -> list[dict]:
        async with self._sm() as session:
            stmt = (
                select(DataSourceModel).where(DataSourceModel.workspace_id == workspace_id).order_by(DataSourceModel.id)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._source_to_public(row) for row in rows]

    async def get_source(self, datasource_id: int, workspace_id: int) -> Optional[dict]:
        async with self._sm() as session:
            stmt = select(DataSourceModel).where(
                DataSourceModel.id == datasource_id,
                DataSourceModel.workspace_id == workspace_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._source_to_public(row) if row else None

    async def get_internal(self, datasource_id: int, workspace_id: int) -> Optional[dict]:
        async with self._sm() as session:
            stmt = select(DataSourceModel).where(
                DataSourceModel.id == datasource_id,
                DataSourceModel.workspace_id == workspace_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._source_to_internal(row) if row else None

    async def delete_source(self, datasource_id: int, workspace_id: int) -> bool:
        async with self._sm() as session:
            stmt = select(DataSourceModel).where(
                DataSourceModel.id == datasource_id,
                DataSourceModel.workspace_id == workspace_id,
            )
            source = (await session.execute(stmt)).scalar_one_or_none()
            if source is None:
                return False
            table_ids = list(
                (
                    await session.execute(
                        select(DataSourceTableModel.id).where(DataSourceTableModel.datasource_id == datasource_id)
                    )
                ).scalars()
            )
            if table_ids:
                await session.execute(
                    delete(DataSourceColumnModel).where(DataSourceColumnModel.table_id.in_(table_ids))
                )
            await session.execute(
                delete(DataSourceTableModel).where(DataSourceTableModel.datasource_id == datasource_id)
            )
            await session.delete(source)
            await session.commit()
            return True

    async def mark_error(self, datasource_id: int, workspace_id: int, error: str) -> None:
        async with self._sm() as session:
            stmt = select(DataSourceModel).where(
                DataSourceModel.id == datasource_id,
                DataSourceModel.workspace_id == workspace_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return
            row.status = "error"
            row.last_error = error[:1000]
            row.updated_at = datetime.utcnow()
            await session.commit()

    async def sync_snapshot(self, datasource_id: int, workspace_id: int, snapshot: SchemaSnapshot) -> dict:
        """事务化 upsert 物理快照，保留未变化对象的审核结果。"""
        now = datetime.utcnow()
        async with self._sm() as session:
            source_stmt = select(DataSourceModel).where(
                DataSourceModel.id == datasource_id,
                DataSourceModel.workspace_id == workspace_id,
            )
            source = (await session.execute(source_stmt)).scalar_one_or_none()
            if source is None:
                raise DataSourceNotFoundError("数据源不存在")

            table_rows = list(
                (
                    await session.execute(
                        select(DataSourceTableModel).where(DataSourceTableModel.datasource_id == datasource_id)
                    )
                ).scalars()
            )
            table_by_key = {(row.schema_name, row.table_name): row for row in table_rows}
            seen_table_ids: set[int] = set()

            for table in snapshot.tables:
                key = (table.schema_name, table.name)
                table_signature = _table_signature(table)
                table_row = table_by_key.get(key)
                if table_row is None:
                    table_row = DataSourceTableModel(
                        datasource_id=datasource_id,
                        schema_name=table.schema_name,
                        table_name=table.name,
                        table_type=table.table_type,
                        physical_comment=table.physical_comment,
                        physical_signature=table_signature,
                        review_status=ReviewStatus.PENDING.value,
                    )
                    session.add(table_row)
                    await session.flush()
                else:
                    if table_row.physical_signature != table_signature:
                        table_row.ai_comment = ""
                        table_row.review_status = ReviewStatus.PENDING.value
                    table_row.table_type = table.table_type
                    table_row.physical_comment = table.physical_comment
                    table_row.physical_signature = table_signature
                    table_row.updated_at = now
                seen_table_ids.add(table_row.id)

                column_rows = list(
                    (
                        await session.execute(
                            select(DataSourceColumnModel).where(DataSourceColumnModel.table_id == table_row.id)
                        )
                    ).scalars()
                )
                column_by_name = {row.column_name: row for row in column_rows}
                seen_column_ids: set[int] = set()
                for column in table.columns:
                    signature = _column_signature(column)
                    column_row = column_by_name.get(column.name)
                    if column_row is None:
                        column_row = DataSourceColumnModel(
                            table_id=table_row.id,
                            column_name=column.name,
                            physical_signature=signature,
                            review_status=ReviewStatus.PENDING.value,
                        )
                        session.add(column_row)
                        await session.flush()
                    elif column_row.physical_signature != signature:
                        column_row.ai_comment = ""
                        column_row.ai_synonyms = []
                        column_row.review_status = ReviewStatus.PENDING.value
                    column_row.data_type = column.data_type
                    column_row.ordinal_position = column.ordinal_position
                    column_row.nullable = column.nullable
                    column_row.primary_key = column.primary_key
                    column_row.physical_comment = column.physical_comment
                    column_row.references = dict(column.references)
                    column_row.physical_signature = signature
                    column_row.updated_at = now
                    seen_column_ids.add(column_row.id)

                removed_column_ids = {row.id for row in column_rows} - seen_column_ids
                if removed_column_ids:
                    await session.execute(
                        delete(DataSourceColumnModel).where(DataSourceColumnModel.id.in_(removed_column_ids))
                    )

            removed_table_ids = {row.id for row in table_rows} - seen_table_ids
            if removed_table_ids:
                await session.execute(
                    delete(DataSourceColumnModel).where(DataSourceColumnModel.table_id.in_(removed_table_ids))
                )
                await session.execute(
                    delete(DataSourceTableModel).where(DataSourceTableModel.id.in_(removed_table_ids))
                )

            source.status = "ready"
            source.last_error = None
            source.schema_hash = _snapshot_hash(snapshot)
            source.last_synced_at = now
            source.updated_at = now
            await session.commit()
            await session.refresh(source)
            return self._source_to_public(source)

    async def get_catalog(self, datasource_id: int, workspace_id: int) -> Optional[dict]:
        async with self._sm() as session:
            source_stmt = select(DataSourceModel).where(
                DataSourceModel.id == datasource_id,
                DataSourceModel.workspace_id == workspace_id,
            )
            source = (await session.execute(source_stmt)).scalar_one_or_none()
            if source is None:
                return None

            table_rows = list(
                (
                    await session.execute(
                        select(DataSourceTableModel)
                        .where(DataSourceTableModel.datasource_id == datasource_id)
                        .order_by(DataSourceTableModel.schema_name, DataSourceTableModel.table_name)
                    )
                ).scalars()
            )
            table_ids = [row.id for row in table_rows]
            column_rows = []
            if table_ids:
                column_rows = list(
                    (
                        await session.execute(
                            select(DataSourceColumnModel)
                            .where(DataSourceColumnModel.table_id.in_(table_ids))
                            .order_by(DataSourceColumnModel.table_id, DataSourceColumnModel.ordinal_position)
                        )
                    ).scalars()
                )
            columns_by_table: dict[int, list[DataSourceColumnModel]] = {}
            for column in column_rows:
                columns_by_table.setdefault(column.table_id, []).append(column)

            tables: list[dict] = []
            for table in table_rows:
                tables.append(
                    {
                        "id": table.id,
                        "schema_name": table.schema_name,
                        "table_name": table.table_name,
                        "table_type": table.table_type,
                        "physical_comment": table.physical_comment,
                        "ai_comment": table.ai_comment,
                        "reviewed_comment": table.reviewed_comment,
                        "review_status": table.review_status,
                        "columns": [
                            {
                                "id": column.id,
                                "column_name": column.column_name,
                                "data_type": column.data_type,
                                "ordinal_position": column.ordinal_position,
                                "nullable": column.nullable,
                                "primary_key": column.primary_key,
                                "physical_comment": column.physical_comment,
                                "ai_comment": column.ai_comment,
                                "reviewed_comment": column.reviewed_comment,
                                "ai_synonyms": list(column.ai_synonyms or []),
                                "reviewed_synonyms": list(column.reviewed_synonyms or []),
                                "references": dict(column.references or {}),
                                "review_status": column.review_status,
                            }
                            for column in columns_by_table.get(table.id, [])
                        ],
                    }
                )
            return {"datasource": self._source_to_public(source), "tables": tables}

    async def save_drafts(self, datasource_id: int, workspace_id: int, drafts: list[TableDraft]) -> None:
        async with self._sm() as session:
            source_exists = (
                await session.execute(
                    select(DataSourceModel.id).where(
                        DataSourceModel.id == datasource_id,
                        DataSourceModel.workspace_id == workspace_id,
                    )
                )
            ).scalar_one_or_none()
            if source_exists is None:
                raise DataSourceNotFoundError("数据源不存在")

            for draft in drafts:
                table = (
                    await session.execute(
                        select(DataSourceTableModel).where(
                            DataSourceTableModel.id == draft.table_id,
                            DataSourceTableModel.datasource_id == datasource_id,
                        )
                    )
                ).scalar_one_or_none()
                if table is None:
                    raise ValueError(f"表不存在或不属于当前数据源: {draft.table_id}")
                table.ai_comment = draft.comment
                table.review_status = ReviewStatus.PENDING.value

                column_rows = list(
                    (
                        await session.execute(
                            select(DataSourceColumnModel).where(DataSourceColumnModel.table_id == table.id)
                        )
                    ).scalars()
                )
                column_by_name = {column.column_name: column for column in column_rows}
                for column_draft in draft.columns:
                    column = column_by_name.get(column_draft.name)
                    if column is None:
                        raise ValueError(f"AI 草稿包含未知字段: {table.table_name}.{column_draft.name}")
                    column.ai_comment = column_draft.comment
                    column.ai_synonyms = list(column_draft.synonyms)
                    column.review_status = ReviewStatus.PENDING.value
            await session.commit()

    async def review_table(
        self,
        *,
        datasource_id: int,
        workspace_id: int,
        table_id: int,
        decision: ReviewStatus,
        table_comment: str | None,
        columns: list[dict],
    ) -> dict:
        async with self._sm() as session:
            source_exists = (
                await session.execute(
                    select(DataSourceModel.id).where(
                        DataSourceModel.id == datasource_id,
                        DataSourceModel.workspace_id == workspace_id,
                    )
                )
            ).scalar_one_or_none()
            if source_exists is None:
                raise DataSourceNotFoundError("数据源不存在")

            table = (
                await session.execute(
                    select(DataSourceTableModel).where(
                        DataSourceTableModel.id == table_id,
                        DataSourceTableModel.datasource_id == datasource_id,
                    )
                )
            ).scalar_one_or_none()
            if table is None:
                raise DataSourceNotFoundError("表不存在")

            column_rows = list(
                (
                    await session.execute(
                        select(DataSourceColumnModel)
                        .where(DataSourceColumnModel.table_id == table_id)
                        .order_by(DataSourceColumnModel.ordinal_position)
                    )
                ).scalars()
            )
            column_by_name = {row.column_name: row for row in column_rows}
            requested = {str(item["name"]): item for item in columns}
            unknown = sorted(set(requested) - set(column_by_name))
            if unknown:
                raise ValueError(f"审核请求包含未知字段: {', '.join(unknown)}")
            if decision == ReviewStatus.APPROVED and set(requested) != set(column_by_name):
                missing = sorted(set(column_by_name) - set(requested))
                raise ValueError(f"批准时必须逐字段确认，缺少: {', '.join(missing)}")

            table.reviewed_comment = (
                str(table_comment).strip() if table_comment is not None else str(table.ai_comment or "").strip()
            )
            table.review_status = decision.value

            for column in column_rows:
                item = requested.get(column.column_name)
                if decision == ReviewStatus.REJECTED:
                    column.review_status = decision.value
                    continue
                assert item is not None
                comment = item.get("comment")
                synonyms = item.get("synonyms")
                column.reviewed_comment = (
                    str(comment).strip() if comment is not None else str(column.ai_comment or "").strip()
                )
                column.reviewed_synonyms = [str(value).strip() for value in (synonyms or []) if str(value).strip()]
                column.review_status = decision.value

            await session.commit()
            return {
                "table_id": table.id,
                "review_status": table.review_status,
                "reviewed_comment": table.reviewed_comment,
                "columns": [
                    {
                        "name": column.column_name,
                        "review_status": column.review_status,
                        "reviewed_comment": column.reviewed_comment,
                        "reviewed_synonyms": list(column.reviewed_synonyms or []),
                    }
                    for column in column_rows
                ],
            }
