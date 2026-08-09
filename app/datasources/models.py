"""数据源领域对象。

领域层刻意不依赖 FastAPI/SQLAlchemy，连接器、服务和测试共用这些结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DataSourceKind(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class DataSourceError(RuntimeError):
    """数据源领域错误的基类。"""


class DataSourceNotFoundError(DataSourceError):
    """当前工作空间内不存在指定数据源。"""


class DataSourceConfigError(DataSourceError):
    """连接参数或部署配置不合法。"""


class DataSourceConnectionError(DataSourceError):
    """连接、扫描或查询失败（异常文本必须先脱敏）。"""


class SemanticDraftError(DataSourceError):
    """AI 语义草稿生成或解析失败。"""


@dataclass(frozen=True)
class ConnectionSpec:
    """运行时完整连接参数；只在内存中短暂存在，不直接序列化到 API。"""

    kind: DataSourceKind
    config: dict[str, Any]
    credentials: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ColumnSnapshot:
    name: str
    data_type: str
    ordinal_position: int
    nullable: bool = True
    primary_key: bool = False
    physical_comment: str = ""
    references: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TableSnapshot:
    schema_name: str
    name: str
    table_type: str
    physical_comment: str
    columns: tuple[ColumnSnapshot, ...]


@dataclass(frozen=True)
class SchemaSnapshot:
    tables: tuple[TableSnapshot, ...]


@dataclass(frozen=True)
class ColumnDraft:
    name: str
    comment: str
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True)
class TableDraft:
    table_id: int
    comment: str
    columns: tuple[ColumnDraft, ...]
