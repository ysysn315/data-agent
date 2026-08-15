"""持久化层 - SQLAlchemy 2.0 Declarative 模型。

对齐 Yuxi 的关键设计（backend/.../postgres/models_business.py:229-273 的 Skill 表）：
**技能正文存文件系统，数据库只存元数据索引**。因此 skills 表刻意不含 content 列，
SKILL.md 正文永远从 dir_path 读（见 repositories.SqlAlchemySkillRepository）。

字段类型选择 SQLite / PostgreSQL 双通用的子集：
- JSON：SQLite 存 TEXT、PG 存 json，SQLAlchemy 自动编解码（列表/字典）
- DateTime：naive UTC，与内存版 InMemorySkillRepository（datetime.utcnow）口径一致
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    """naive UTC，与内存版 datetime.utcnow() 保持同一口径。"""
    return datetime.utcnow()


class Base(DeclarativeBase):
    """所有持久化模型的声明式基类（一处 metadata，便于 create_all）。"""


class SkillModel(Base):
    """skills 表：技能元数据索引（正文不入库，从 dir_path 读）。

    对齐 app/skills/models.py 的 Skill 领域模型，但**没有 content 列**：
    正文、依赖声明都从 dir_path/SKILL.md 现读现解析（渐进式披露的数据源单一化）。
    仅承载 upload / remote 两类技能；builtin 由 SkillService 从文件系统加载进缓存。
    """

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 技能目录路径；正文永远从这里的 SKILL.md 读，数据库不存正文
    dir_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="upload", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    share_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class MCPServerModel(Base):
    """mcp_servers 表：MCP server 注册信息（字段对齐 app/mcp/models.MCPServer 全字段）。"""

    __tablename__ = "mcp_servers"

    slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    transport: Mapped[str] = mapped_column(String(32), nullable=False)

    # http 类 transport
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    headers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    timeout: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sse_read_timeout: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # stdio transport
    command: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    args: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    env: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    disabled_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class SQLExampleModel(Base):
    """sql_examples 表：question→SQL 示例库（few-shot 运营闭环）。

    example_id 直接用领域层生成的 12 位 hex 作主键（与内存版 dict 的 "id" 一致）。
    verified=False 即候选示例（对话沉淀待确认 / 评测失败导入），人工转正后进 few-shot。
    datasource_id NULL=演示库全局作用域；workspace_id 0=内置种子/无鉴权 demo。
    """

    __tablename__ = "sql_examples"

    example_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    datasource_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class TerminologyModel(Base):
    """terminology 表：业务术语库（作用域内唯一，同义词/口径存 JSON）。

    唯一键 (term, datasource_id, workspace_id)：同一术语可在不同作用域各自存在
    （数据源 11 与 22 各配各的 GMV 口径），跨作用域互不覆盖。
    作用域列语义同 sql_examples：datasource_id NULL=演示全局；workspace_id 0=种子/demo。
    """

    __tablename__ = "terminology"
    __table_args__ = (UniqueConstraint("term", "datasource_id", "workspace_id", name="uq_terminology_scope_term"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    synonyms: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    definition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sql_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    datasource_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


# ========== 知识图谱（平台化扩展） ==========


class GraphTripleModel(Base):
    """作用域内的知识图谱关系。

    ``subject/object`` 仍保留为接口兼容和可读快照；新代码同时记录实体 ID，
    NetworkX 查询镜像按 ``scope_key`` 构建，避免不同 workspace/datasource 串图。
    """

    __tablename__ = "graph_triples"
    __table_args__ = (
        UniqueConstraint("scope_key", "subject", "predicate", "object", name="uq_graph_triples_scope_spo"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    predicate: Mapped[str] = mapped_column(String(256), nullable=False)
    object: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False, default="workspace:0", index=True)
    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    datasource_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    subject_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    object_entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 来源标记：seed（首启种子）/ manual（API 手动补录）/ llm（LLM 抽取）/ schema_reviewed
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    # source 保留兼容旧接口；source_type 是可扩展的来源分类，confidence 是事实置信度。
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class GraphEntityModel(Base):
    """作用域内的实体主表。

    图结构边仍保存在 ``graph_triples``，本表负责稳定实体 ID、规范名称、别名、
    轻量属性和合并状态。``attributes`` 保留多值及 provenance，避免 LLM 草稿
    覆盖人工审核结果。
    """

    __tablename__ = "graph_entities"
    __table_args__ = (UniqueConstraint("scope_key", "normalized_name", name="uq_graph_entities_scope_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    datasource_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    canonical_name: Mapped[str] = mapped_column(String(256), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(256), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    aliases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    merged_into_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    embedding_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    embedding_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class GraphEntityAliasModel(Base):
    """实体别名索引，供作用域内精确消歧。"""

    __tablename__ = "graph_entity_aliases"
    __table_args__ = (UniqueConstraint("scope_key", "normalized_alias", name="uq_graph_entity_alias_scope_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(256), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(256), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    confidence: Mapped[float] = mapped_column(default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


# ========== 用户体系 + 工作空间（F 轮追加；就近声明，不改动文件头部） ==========


class WorkspaceModel(Base):
    """workspaces 表：多租户-lite 的资源隔离单元（对齐 SQLBot sys_workspace）。

    lite 版只保留最小三列：slug（人类可读唯一键，如 default）/ name / created_at；
    不做部门树、不做成员关系表（一个用户属于一个工作空间，见 UserModel.workspace_id）。
    参考 sqlbot-reference alembic/versions/020_workspace_ddl.py 的 sys_workspace(id/name/create_time)。
    """

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class UserModel(Base):
    """users 表：API Key 鉴权的最小用户模型（对齐 Yuxi User + APIKey 的收敛版）。

    Yuxi 把 User 与 APIKey 拆两张表（一个用户可多把 Key、含 expires_at/last_used_at）；
    本项目 lite 版**一个用户一把 Key**，直接内联在 users 表，省一张关联表与一次 join：
    - api_key_hash：sha256(明文) 的 64 位 hex，**只存哈希**（对齐 Yuxi APIKey.key_hash）。
      明文 Key 仅在创建响应/bootstrap 日志里出现一次，库里永不留明文。
    - api_key_prefix：明文前 8 位（da- + 5 hex），便于在 UI/日志里识别是哪把 Key（对齐 Yuxi key_prefix）。
    - role：admin | member（对齐 Yuxi role，但砍掉 superadmin 层）。
    - workspace_id：用户所属工作空间（裸 Integer + index，不设 FK，与既有 skills.user_id 同风格，
      SQLite 起步免 FK 建表顺序/级联的心智负担；PG 化时再补 FK）。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")
    workspace_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    api_key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


# ========== 外部数据源 + 语义元数据 ==========


class DataSourceModel(Base):
    """用户接入的数据源。

    connection_config 只保存主机、端口、库名、schema 或受限 SQLite 路径等非敏感字段；
    用户名/密码整体加密后放在 encrypted_credentials，任何 API 投影都不得返回后者。
    workspace_id=0 是关闭鉴权时的 demo 工作空间，启用鉴权后使用真实工作空间 ID。
    """

    __tablename__ = "data_sources"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_data_sources_workspace_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    connection_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class DataSourceTableModel(Base):
    """数据源中的表/视图及三层注释：物理注释、AI 草稿、人工审核结果。"""

    __tablename__ = "data_source_tables"
    __table_args__ = (UniqueConstraint("datasource_id", "schema_name", "table_name", name="uq_datasource_table_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    datasource_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    schema_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    table_name: Mapped[str] = mapped_column(String(256), nullable=False)
    table_type: Mapped[str] = mapped_column(String(32), nullable=False, default="table")
    physical_comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ai_comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    physical_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class DataSourceColumnModel(Base):
    """表字段的物理信息、AI 语义草稿、已审核语义和真实外键关系。"""

    __tablename__ = "data_source_columns"
    __table_args__ = (UniqueConstraint("table_id", "column_name", name="uq_datasource_column_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    column_name: Mapped[str] = mapped_column(String(256), nullable=False)
    data_type: Mapped[str] = mapped_column(String(256), nullable=False, default="UNKNOWN")
    ordinal_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nullable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    primary_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    physical_comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ai_comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ai_synonyms: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    reviewed_synonyms: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    references: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    physical_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
