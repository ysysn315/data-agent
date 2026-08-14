"""持久化层 - async 数据访问层（Repository）

设计对齐 Yuxi agents/skills/repository.py 的**薄仓储 + autocommit-per-operation**：
每个方法自开会话、自提交，不把事务边界泄漏给上层（利：调用简单、无长事务；
弊：跨方法的原子性需上层自己保证——本项目的技能/注册表操作粒度都是单条，够用）。

四个仓储：
- SqlAlchemySkillRepository：**与 app/skills/repository.InMemorySkillRepository 方法集完全一致**，
  是 SkillService 依赖的接口契约。正文不入库，读时从 dir_path/SKILL.md 现读（对齐 Yuxi）。
- MCPRepository / SQLExampleRepository / TerminologyRepository：
  为 MCPService / ExampleStore / TermStore 的「DB 版」存储后端，
  提供 list_all / replace_all（整表替换，对应 JSON 版的整文件原子重写）+ 单条增删查。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    GraphEntityAliasModel,
    GraphEntityModel,
    GraphTripleModel,
    MCPServerModel,
    SkillModel,
    SQLExampleModel,
    TerminologyModel,
)
from app.mcp.models import MCPServer
from app.skills.models import Skill, SkillSourceType

# ========== 技能仓储（InMemorySkillRepository 契约的 SQLAlchemy 实现） ==========


class SqlAlchemySkillRepository:
    """技能数据访问层（数据库版）。

    方法集与 InMemorySkillRepository 一一对应，SkillService 无感切换。
    仅承载 upload / remote 技能；builtin 由 SkillService 从文件系统加载进缓存、不入库。
    """

    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    # ---------- 领域对象 <-> 行 ----------

    @staticmethod
    def _read_content(dir_path: Optional[str]) -> str:
        """正文永远从 dir_path/SKILL.md 现读（数据库不存正文，对齐 Yuxi）。"""
        if not dir_path:
            return ""
        skill_file = Path(dir_path) / "SKILL.md"
        try:
            return skill_file.read_text(encoding="utf-8") if skill_file.exists() else ""
        except OSError:
            return ""

    def _row_to_skill(self, row: SkillModel) -> Skill:
        return Skill(
            id=row.id,
            slug=row.slug,
            name=row.name,
            description=row.description,
            content=self._read_content(row.dir_path),
            dir_path=row.dir_path,
            source_type=SkillSourceType(row.source_type),
            enabled=row.enabled,
            user_id=row.user_id,
            share_config=dict(row.share_config or {}),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _apply(row: SkillModel, skill: Skill) -> None:
        row.name = skill.name
        row.description = skill.description
        row.dir_path = skill.dir_path
        row.source_type = skill.source_type.value
        row.enabled = skill.enabled
        row.user_id = skill.user_id
        row.share_config = dict(skill.share_config or {})

    # ---------- 查询 ----------

    async def get_by_id(self, skill_id: int) -> Optional[Skill]:
        async with self._sm() as session:
            row = await session.get(SkillModel, skill_id)
            return self._row_to_skill(row) if row else None

    async def get_by_slug(self, slug: str) -> Optional[Skill]:
        async with self._sm() as session:
            stmt = select(SkillModel).where(SkillModel.slug == slug)
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._row_to_skill(row) if row else None

    async def list_all(
        self,
        enabled_only: bool = False,
        source_type: Optional[SkillSourceType] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Skill]:
        async with self._sm() as session:
            stmt = select(SkillModel)
            if enabled_only:
                stmt = stmt.where(SkillModel.enabled.is_(True))
            if source_type:
                stmt = stmt.where(SkillModel.source_type == source_type.value)
            stmt = stmt.order_by(SkillModel.id).offset(offset).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [self._row_to_skill(r) for r in rows]

    async def list_enabled(self) -> list[Skill]:
        return await self.list_all(enabled_only=True)

    async def list_accessible_by_user(
        self,
        user_id: int,
        include_global: bool = True,
    ) -> list[Skill]:
        async with self._sm() as session:
            stmt = select(SkillModel).where(SkillModel.enabled.is_(True))
            if include_global:
                stmt = stmt.where(or_(SkillModel.user_id.is_(None), SkillModel.user_id == user_id))
            else:
                stmt = stmt.where(SkillModel.user_id == user_id)
            stmt = stmt.order_by(SkillModel.id)
            rows = (await session.execute(stmt)).scalars().all()
            return [self._row_to_skill(r) for r in rows]

    # ---------- 增删改 ----------

    async def create(self, skill: Skill) -> Skill:
        async with self._sm() as session:
            exists = (await session.execute(select(SkillModel.id).where(SkillModel.slug == skill.slug))).first()
            if exists:
                raise ValueError(f"Skill slug 已存在: {skill.slug}")

            now = datetime.utcnow()
            row = SkillModel(slug=skill.slug, created_at=now, updated_at=now)
            self._apply(row, skill)
            session.add(row)
            await session.commit()
            await session.refresh(row)

            # 回填 id/时间戳，正文等内存字段保持不变（正文本就不入库）
            skill.id = row.id
            skill.created_at = row.created_at
            skill.updated_at = row.updated_at
            return skill

    async def update(self, skill: Skill) -> Skill:
        async with self._sm() as session:
            stmt = select(SkillModel).where(SkillModel.slug == skill.slug)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                raise ValueError(f"Skill 不存在: {skill.slug}")

            self._apply(row, skill)
            row.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(row)

            skill.id = row.id
            skill.updated_at = row.updated_at
            if skill.created_at is None:
                skill.created_at = row.created_at
            return skill

    async def delete(self, slug: str) -> bool:
        async with self._sm() as session:
            stmt = select(SkillModel).where(SkillModel.slug == slug)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def enable(self, slug: str) -> bool:
        return await self._set_enabled(slug, True)

    async def disable(self, slug: str) -> bool:
        return await self._set_enabled(slug, False)

    async def _set_enabled(self, slug: str, enabled: bool) -> bool:
        async with self._sm() as session:
            stmt = select(SkillModel).where(SkillModel.slug == slug)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return False
            row.enabled = enabled
            row.updated_at = datetime.utcnow()
            await session.commit()
            return True

    async def exists(self, slug: str) -> bool:
        async with self._sm() as session:
            stmt = select(SkillModel.id).where(SkillModel.slug == slug)
            return (await session.execute(stmt)).first() is not None

    async def clear(self) -> None:
        async with self._sm() as session:
            await session.execute(delete(SkillModel))
            await session.commit()

    async def count(self) -> int:
        async with self._sm() as session:
            stmt = select(func.count()).select_from(SkillModel)
            return int((await session.execute(stmt)).scalar_one())


# ========== MCP 注册表仓储 ==========


class MCPRepository:
    """MCP server 注册表存储后端（供 MCPService 的 DB 版）。"""

    _FIELDS = (
        "name",
        "description",
        "transport",
        "url",
        "headers",
        "timeout",
        "sse_read_timeout",
        "command",
        "args",
        "env",
        "enabled",
        "disabled_tools",
    )

    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    @staticmethod
    def _to_server(row: MCPServerModel) -> MCPServer:
        return MCPServer(
            slug=row.slug,
            name=row.name,
            description=row.description,
            transport=row.transport,
            url=row.url,
            headers=dict(row.headers or {}),
            timeout=row.timeout,
            sse_read_timeout=row.sse_read_timeout,
            command=row.command,
            args=list(row.args or []),
            env=dict(row.env or {}),
            enabled=row.enabled,
            disabled_tools=list(row.disabled_tools or []),
        )

    @classmethod
    def _apply(cls, row: MCPServerModel, server: MCPServer) -> None:
        for field in cls._FIELDS:
            setattr(row, field, getattr(server, field))

    async def list_all(self) -> list[MCPServer]:
        async with self._sm() as session:
            stmt = select(MCPServerModel).order_by(MCPServerModel.slug)
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_server(r) for r in rows]

    async def get(self, slug: str) -> Optional[MCPServer]:
        async with self._sm() as session:
            row = await session.get(MCPServerModel, slug)
            return self._to_server(row) if row else None

    async def upsert(self, server: MCPServer) -> None:
        async with self._sm() as session:
            row = await session.get(MCPServerModel, server.slug)
            if row is None:
                row = MCPServerModel(slug=server.slug)
                session.add(row)
            self._apply(row, server)
            await session.commit()

    async def delete(self, slug: str) -> bool:
        async with self._sm() as session:
            row = await session.get(MCPServerModel, slug)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def replace_all(self, servers: list[MCPServer]) -> None:
        async with self._sm() as session:
            await session.execute(delete(MCPServerModel))
            for server in servers:
                row = MCPServerModel(slug=server.slug)
                self._apply(row, server)
                session.add(row)
            await session.commit()

    async def count(self) -> int:
        async with self._sm() as session:
            stmt = select(func.count()).select_from(MCPServerModel)
            return int((await session.execute(stmt)).scalar_one())


# ========== SQL 示例库仓储 ==========


class SQLExampleRepository:
    """SQL 示例库存储后端（供 ExampleStore 的 DB 版）。领域层用 dict，故仓储也收发 dict。"""

    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    @staticmethod
    def _to_dict(row: SQLExampleModel) -> dict:
        return {
            "id": row.example_id,
            "question": row.question,
            "sql": row.sql,
            "verified": bool(row.verified),
        }

    async def list_all(self) -> list[dict]:
        async with self._sm() as session:
            stmt = select(SQLExampleModel).order_by(SQLExampleModel.created_at, SQLExampleModel.example_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_dict(r) for r in rows]

    async def upsert(self, rec: dict) -> None:
        async with self._sm() as session:
            row = await session.get(SQLExampleModel, rec["id"])
            if row is None:
                row = SQLExampleModel(example_id=rec["id"])
                session.add(row)
            row.question = rec["question"]
            row.sql = rec["sql"]
            row.verified = bool(rec.get("verified", True))
            await session.commit()

    async def delete(self, example_id: str) -> bool:
        async with self._sm() as session:
            row = await session.get(SQLExampleModel, example_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def replace_all(self, records: list[dict]) -> None:
        async with self._sm() as session:
            await session.execute(delete(SQLExampleModel))
            for rec in records:
                session.add(
                    SQLExampleModel(
                        example_id=rec["id"],
                        question=rec["question"],
                        sql=rec["sql"],
                        verified=bool(rec.get("verified", True)),
                    )
                )
            await session.commit()

    async def count(self) -> int:
        async with self._sm() as session:
            stmt = select(func.count()).select_from(SQLExampleModel)
            return int((await session.execute(stmt)).scalar_one())


# ========== 术语库仓储 ==========


class TerminologyRepository:
    """业务术语库存储后端（供 TermStore 的 DB 版）。term 为主键。"""

    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    @staticmethod
    def _to_dict(row: TerminologyModel) -> dict:
        return {
            "term": row.term,
            "synonyms": list(row.synonyms or []),
            "definition": row.definition,
            "sql_hint": row.sql_hint,
        }

    async def list_all(self) -> list[dict]:
        async with self._sm() as session:
            stmt = select(TerminologyModel).order_by(TerminologyModel.created_at, TerminologyModel.term)
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_dict(r) for r in rows]

    async def upsert(self, rec: dict) -> None:
        async with self._sm() as session:
            row = await session.get(TerminologyModel, rec["term"])
            if row is None:
                row = TerminologyModel(term=rec["term"])
                session.add(row)
            row.synonyms = list(rec.get("synonyms") or [])
            row.definition = rec.get("definition") or ""
            row.sql_hint = rec.get("sql_hint")
            await session.commit()

    async def delete(self, term: str) -> bool:
        async with self._sm() as session:
            row = await session.get(TerminologyModel, term)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def replace_all(self, records: list[dict]) -> None:
        async with self._sm() as session:
            await session.execute(delete(TerminologyModel))
            for rec in records:
                session.add(
                    TerminologyModel(
                        term=rec["term"],
                        synonyms=list(rec.get("synonyms") or []),
                        definition=rec.get("definition") or "",
                        sql_hint=rec.get("sql_hint"),
                    )
                )
            await session.commit()

    async def count(self) -> int:
        async with self._sm() as session:
            stmt = select(func.count()).select_from(TerminologyModel)
            return int((await session.execute(stmt)).scalar_one())


# ========== 知识图谱仓储（平台化扩展） ==========


class GraphEntityRepository:
    """实体/别名的轻量仓储。

    GraphStore 的同步门面通过 ``run_sync`` 调用本仓储；方法自身保持 async，方便
    API 或后台任务直接复用。实体写入采用先查后插，规模假设仍是千级图谱。
    """

    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    @staticmethod
    def _to_dict(row: GraphEntityModel) -> dict:
        return {
            "id": row.id,
            "scope_key": row.scope_key,
            "workspace_id": row.workspace_id,
            "datasource_id": row.datasource_id,
            "canonical_name": row.canonical_name,
            "normalized_name": row.normalized_name,
            "entity_type": row.entity_type,
            "aliases": list(row.aliases or []),
            "attributes": dict(row.attributes or {}),
            "status": row.status,
            "merged_into_id": row.merged_into_id,
            "embedding_status": row.embedding_status,
            "embedding_hash": row.embedding_hash,
        }

    async def list_scope(self, scope_key: str) -> list[dict]:
        async with self._sm() as session:
            stmt = (
                select(GraphEntityModel)
                .where(GraphEntityModel.scope_key == scope_key, GraphEntityModel.status == "active")
                .order_by(GraphEntityModel.id)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_dict(row) for row in rows]

    async def get_by_id(self, entity_id: int, scope_key: str | None = None) -> dict | None:
        async with self._sm() as session:
            stmt = select(GraphEntityModel).where(GraphEntityModel.id == entity_id)
            if scope_key is not None:
                stmt = stmt.where(GraphEntityModel.scope_key == scope_key)
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._to_dict(row) if row else None

    async def get_by_normalized(self, scope_key: str, normalized_name: str) -> dict | None:
        async with self._sm() as session:
            stmt = select(GraphEntityModel).where(
                GraphEntityModel.scope_key == scope_key,
                GraphEntityModel.normalized_name == normalized_name,
                GraphEntityModel.status == "active",
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._to_dict(row) if row else None

    async def get_by_alias(self, scope_key: str, normalized_alias: str) -> dict | None:
        async with self._sm() as session:
            stmt = select(GraphEntityAliasModel.entity_id).where(
                GraphEntityAliasModel.scope_key == scope_key,
                GraphEntityAliasModel.normalized_alias == normalized_alias,
            )
            entity_id = (await session.execute(stmt)).scalar_one_or_none()
            if entity_id is None:
                return None
            row = await session.get(GraphEntityModel, entity_id)
            return self._to_dict(row) if row and row.status == "active" else None

    async def upsert_many(self, entities: list[dict]) -> list[dict]:
        if not entities:
            return []
        from app.graph.entities import merge_attributes, normalize_entity_name

        async with self._sm() as session:
            result: list[dict] = []
            for item in entities:
                name = str(item.get("canonical_name") or item.get("name") or "").strip()
                if not name:
                    continue
                scope_key = item.get("scope_key") or "workspace:0"
                normalized = item.get("normalized_name") or normalize_entity_name(name)
                stmt = select(GraphEntityModel).where(
                    GraphEntityModel.scope_key == scope_key,
                    GraphEntityModel.normalized_name == normalized,
                    GraphEntityModel.status == "active",
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                source = item.get("source") or "manual"
                aliases = [str(alias).strip() for alias in item.get("aliases", []) if str(alias).strip()]
                if row is None:
                    row = GraphEntityModel(
                        scope_key=scope_key,
                        workspace_id=int(item.get("workspace_id") or 0),
                        datasource_id=item.get("datasource_id"),
                        canonical_name=name,
                        normalized_name=normalized,
                        entity_type=item.get("entity_type") or "unknown",
                        aliases=list(dict.fromkeys(aliases)),
                        attributes=dict(item.get("attributes") or {}),
                        embedding_status="pending",
                    )
                    session.add(row)
                    await session.flush()
                else:
                    merged_aliases = list(dict.fromkeys([*(row.aliases or []), *aliases]))
                    row.aliases = merged_aliases
                    row.attributes = merge_attributes(row.attributes, item.get("attributes"), source)
                    if row.entity_type == "unknown" and item.get("entity_type"):
                        row.entity_type = item["entity_type"]
                    row.embedding_status = "pending"
                for alias in aliases:
                    normalized_alias = normalize_entity_name(alias)
                    if not normalized_alias or normalized_alias == row.normalized_name:
                        continue
                    alias_stmt = select(GraphEntityAliasModel).where(
                        GraphEntityAliasModel.scope_key == scope_key,
                        GraphEntityAliasModel.normalized_alias == normalized_alias,
                    )
                    alias_row = (await session.execute(alias_stmt)).scalar_one_or_none()
                    if alias_row is None:
                        session.add(
                            GraphEntityAliasModel(
                                scope_key=scope_key,
                                entity_id=row.id,
                                alias=alias,
                                normalized_alias=normalized_alias,
                                source=source,
                                confidence=float(item.get("confidence") or 1.0),
                            )
                        )
                result.append(self._to_dict(row))
            await session.commit()
            return result

    async def update_embedding_status(self, entity_id: int, status: str, vector_hash: str = "") -> None:
        async with self._sm() as session:
            row = await session.get(GraphEntityModel, entity_id)
            if row:
                row.embedding_status = status
                if vector_hash:
                    row.embedding_hash = vector_hash
                await session.commit()

    async def merge_entities(self, scope_key: str, survivor_id: int, duplicate_id: int) -> dict:
        """显式合并实体并在同一事务内重指关系端点。"""

        from app.graph.entities import merge_attributes

        async with self._sm() as session:
            survivor = await session.get(GraphEntityModel, survivor_id)
            duplicate = await session.get(GraphEntityModel, duplicate_id)
            if not survivor or not duplicate or survivor.scope_key != scope_key or duplicate.scope_key != scope_key:
                raise ValueError("实体不存在或不属于当前图谱作用域")
            if survivor.id == duplicate.id:
                return self._to_dict(survivor)

            survivor.aliases = list(
                dict.fromkeys([*(survivor.aliases or []), duplicate.canonical_name, *(duplicate.aliases or [])])
            )
            survivor.attributes = merge_attributes(survivor.attributes, duplicate.attributes, "manual")

            alias_rows = (
                (
                    await session.execute(
                        select(GraphEntityAliasModel).where(
                            GraphEntityAliasModel.scope_key == scope_key,
                            GraphEntityAliasModel.entity_id == duplicate_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for alias_row in alias_rows:
                exists = (
                    await session.execute(
                        select(GraphEntityAliasModel).where(
                            GraphEntityAliasModel.scope_key == scope_key,
                            GraphEntityAliasModel.normalized_alias == alias_row.normalized_alias,
                            GraphEntityAliasModel.id != alias_row.id,
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    alias_row.entity_id = survivor_id
                else:
                    await session.delete(alias_row)

            triples = (
                (
                    await session.execute(
                        select(GraphTripleModel)
                        .where(GraphTripleModel.scope_key == scope_key)
                        .order_by(GraphTripleModel.id)
                    )
                )
                .scalars()
                .all()
            )
            seen: set[tuple[str, str, str]] = set()
            updates: list[tuple[GraphTripleModel, str, str]] = []
            duplicates: list[GraphTripleModel] = []
            for triple in triples:
                new_subject = (
                    survivor.canonical_name
                    if triple.subject_entity_id == duplicate_id or triple.subject == duplicate.canonical_name
                    else triple.subject
                )
                new_object = (
                    survivor.canonical_name
                    if triple.object_entity_id == duplicate_id or triple.object == duplicate.canonical_name
                    else triple.object
                )
                key = (new_subject, triple.predicate, new_object)
                if key in seen:
                    duplicates.append(triple)
                else:
                    seen.add(key)
                    updates.append((triple, new_subject, new_object))

            # 先删掉合并后会冲突的边并 flush，再更新保留边，避免 SQLite/PG 的
            # ``scope + subject + predicate + object`` 唯一约束被临时撞上。
            for triple in duplicates:
                await session.delete(triple)
            if duplicates:
                await session.flush()
            for triple, subject, object_ in updates:
                if triple.subject_entity_id == duplicate_id or triple.subject == duplicate.canonical_name:
                    triple.subject_entity_id = survivor_id
                if triple.object_entity_id == duplicate_id or triple.object == duplicate.canonical_name:
                    triple.object_entity_id = survivor_id
                triple.subject = subject
                triple.object = object_

            duplicate.status = "merged"
            duplicate.merged_into_id = survivor_id
            duplicate.embedding_status = "stale"
            survivor.embedding_status = "pending"
            await session.commit()
            return self._to_dict(survivor)


class GraphTripleRepository:
    """作用域三元组仓储，保留旧的无参数调用语义（默认 workspace:0）。"""

    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    @staticmethod
    def _to_dict(row: GraphTripleModel) -> dict:
        data = {
            "subject": row.subject,
            "predicate": row.predicate,
            "object": row.object,
            "source": row.source,
        }
        if row.scope_key != "workspace:0":
            data.update(
                scope_key=row.scope_key,
                workspace_id=row.workspace_id,
                datasource_id=row.datasource_id,
            )
        if row.source_ref:
            data["source_ref"] = row.source_ref
        if row.provenance:
            data["provenance"] = dict(row.provenance)
        return data

    async def list_all(self, scope_key: str | None = None) -> list[dict]:
        async with self._sm() as session:
            stmt = select(GraphTripleModel)
            if scope_key is not None:
                stmt = stmt.where(GraphTripleModel.scope_key == scope_key)
            rows = (await session.execute(stmt.order_by(GraphTripleModel.id))).scalars().all()
            return [self._to_dict(r) for r in rows]

    async def add_many(self, triples: list[dict], scope_key: str = "workspace:0") -> int:
        """批量入库，幂等范围为 ``scope_key + (s,p,o)``。"""
        if not triples:
            return 0
        async with self._sm() as session:
            stmt = select(GraphTripleModel.subject, GraphTripleModel.predicate, GraphTripleModel.object).where(
                GraphTripleModel.scope_key == scope_key
            )
            existing = {tuple(row) for row in (await session.execute(stmt)).all()}
            added = 0
            for t in triples:
                key = (t["subject"], t["predicate"], t["object"])
                if key in existing:
                    continue
                session.add(
                    GraphTripleModel(
                        subject=t["subject"],
                        predicate=t["predicate"],
                        object=t["object"],
                        scope_key=t.get("scope_key") or scope_key,
                        workspace_id=int(t.get("workspace_id") or 0),
                        datasource_id=t.get("datasource_id"),
                        subject_entity_id=t.get("subject_entity_id"),
                        object_entity_id=t.get("object_entity_id"),
                        source=t.get("source") or "manual",
                        source_ref=t.get("source_ref"),
                        provenance=dict(t.get("provenance") or {}),
                    )
                )
                existing.add(key)
                added += 1
            await session.commit()
            return added

    async def count(self, scope_key: str | None = None) -> int:
        async with self._sm() as session:
            stmt = select(func.count()).select_from(GraphTripleModel)
            if scope_key is not None:
                stmt = stmt.where(GraphTripleModel.scope_key == scope_key)
            return int((await session.execute(stmt)).scalar_one())


# ========== 用户体系 + 工作空间仓储（F 轮追加；import 就近声明，不改动文件头部） ==========

from app.db.models import UserModel, WorkspaceModel  # noqa: E402


class WorkspaceRepository:
    """工作空间存储后端（供鉴权服务 app/core/auth.py）。

    薄仓储、autocommit-per-operation，收发 dict（与示例/术语/图谱仓储同风格）。
    """

    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    @staticmethod
    def _to_dict(row: WorkspaceModel) -> dict:
        return {"id": row.id, "slug": row.slug, "name": row.name}

    async def get_by_slug(self, slug: str) -> Optional[dict]:
        async with self._sm() as session:
            stmt = select(WorkspaceModel).where(WorkspaceModel.slug == slug)
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._to_dict(row) if row else None

    async def get_by_id(self, workspace_id: int) -> Optional[dict]:
        async with self._sm() as session:
            row = await session.get(WorkspaceModel, workspace_id)
            return self._to_dict(row) if row else None

    async def create(self, slug: str, name: str = "") -> dict:
        async with self._sm() as session:
            exists = (await session.execute(select(WorkspaceModel.id).where(WorkspaceModel.slug == slug))).first()
            if exists:
                raise ValueError(f"工作空间 slug 已存在: {slug}")
            row = WorkspaceModel(slug=slug, name=name or slug)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._to_dict(row)

    async def get_or_create(self, slug: str, name: str = "") -> dict:
        """按 slug 取，不存在则建（bootstrap / 建用户时保证工作空间存在）。"""
        existing = await self.get_by_slug(slug)
        if existing:
            return existing
        try:
            return await self.create(slug, name)
        except ValueError:
            # 并发下他人抢先建了：回读即可（slug 唯一约束兜底）
            return await self.get_by_slug(slug)

    async def list_all(self) -> list[dict]:
        async with self._sm() as session:
            stmt = select(WorkspaceModel).order_by(WorkspaceModel.id)
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_dict(r) for r in rows]

    async def count(self) -> int:
        async with self._sm() as session:
            stmt = select(func.count()).select_from(WorkspaceModel)
            return int((await session.execute(stmt)).scalar_one())


class UserRepository:
    """用户存储后端（供鉴权服务 app/core/auth.py）。

    一个用户内联一把 API Key（api_key_hash/prefix）。**对外一律返回不含哈希的安全 dict**
    （_to_public）；只有 verify_api_key 需要拿哈希做 constant-time 复核，专走 get_by_api_key_hash，
    该方法返回含哈希的内部 dict，调用点用完即弃、不外泄。
    """

    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    @staticmethod
    def _to_public(row: UserModel) -> dict:
        """安全投影：不含 api_key_hash（列表/详情/鉴权成功后统一用这个）。"""
        return {
            "id": row.id,
            "username": row.username,
            "role": row.role,
            "workspace_id": row.workspace_id,
            "api_key_prefix": row.api_key_prefix,
            "enabled": row.enabled,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    async def create(
        self,
        username: str,
        role: str,
        workspace_id: Optional[int],
        api_key_hash: str,
        api_key_prefix: str,
    ) -> dict:
        async with self._sm() as session:
            exists = (await session.execute(select(UserModel.id).where(UserModel.username == username))).first()
            if exists:
                raise ValueError(f"用户名已存在: {username}")
            row = UserModel(
                username=username,
                role=role,
                workspace_id=workspace_id,
                api_key_hash=api_key_hash,
                api_key_prefix=api_key_prefix,
                enabled=True,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._to_public(row)

    async def get_by_id(self, user_id: int) -> Optional[dict]:
        async with self._sm() as session:
            row = await session.get(UserModel, user_id)
            return self._to_public(row) if row else None

    async def get_by_username(self, username: str) -> Optional[dict]:
        async with self._sm() as session:
            stmt = select(UserModel).where(UserModel.username == username)
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._to_public(row) if row else None

    async def get_by_api_key_hash(self, api_key_hash: str) -> Optional[dict]:
        """按哈希取用户（含哈希的内部 dict，仅供 verify_api_key 复核，勿外泄）。"""
        async with self._sm() as session:
            stmt = select(UserModel).where(UserModel.api_key_hash == api_key_hash)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            pub = self._to_public(row)
            pub["api_key_hash"] = row.api_key_hash
            return pub

    async def list_all(self) -> list[dict]:
        async with self._sm() as session:
            stmt = select(UserModel).order_by(UserModel.id)
            rows = (await session.execute(stmt)).scalars().all()
            return [self._to_public(r) for r in rows]

    async def set_enabled(self, user_id: int, enabled: bool) -> Optional[dict]:
        async with self._sm() as session:
            row = await session.get(UserModel, user_id)
            if row is None:
                return None
            row.enabled = enabled
            await session.commit()
            await session.refresh(row)
            return self._to_public(row)

    async def count(self) -> int:
        async with self._sm() as session:
            stmt = select(func.count()).select_from(UserModel)
            return int((await session.execute(stmt)).scalar_one())
