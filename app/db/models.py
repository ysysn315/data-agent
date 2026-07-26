"""持久化层 - SQLAlchemy 2.0 Declarative 模型（五张表）

对齐 Yuxi 的关键设计（backend/.../postgres/models_business.py:229-273 的 Skill 表）：
**技能正文存文件系统，数据库只存元数据索引**。因此 skills 表刻意不含 content 列，
SKILL.md 正文永远从 dir_path 读（见 repositories.SqlAlchemySkillRepository）。

字段类型选择 SQLite / PostgreSQL 双通用的子集：
- JSON：SQLite 存 TEXT、PG 存 json，SQLAlchemy 自动编解码（列表/字典）
- DateTime：naive UTC，与内存版 InMemorySkillRepository（datetime.utcnow）口径一致
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


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
    """

    __tablename__ = "sql_examples"

    example_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class TerminologyModel(Base):
    """terminology 表：业务术语库（term 为唯一键，同义词/口径存 JSON）。"""

    __tablename__ = "terminology"

    term: Mapped[str] = mapped_column(String(256), primary_key=True)
    synonyms: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    definition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sql_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


# ========== 知识图谱（E 轮追加；本段 import 就近声明，不改动文件头部） ==========

from sqlalchemy import UniqueConstraint  # noqa: E402


class GraphTripleModel(Base):
    """graph_triples 表：知识图谱三元组（轻量版图谱的唯一持久化层）。

    (subject, predicate, object) 唯一约束保证入库幂等；NetworkX 内存图是本表的
    只读镜像（app/graph/store.GraphStore 惰性重建），不另设图数据库（取舍见
    app/graph/IMPLEMENTATION.md §4，Neo4j 升级路径预留在文档里）。
    """

    __tablename__ = "graph_triples"
    __table_args__ = (
        UniqueConstraint("subject", "predicate", "object", name="uq_graph_triples_spo"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    predicate: Mapped[str] = mapped_column(String(256), nullable=False)
    object: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    # 来源标记：seed（首启种子）/ manual（API 手动补录）/ llm（LLM 抽取）
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
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
