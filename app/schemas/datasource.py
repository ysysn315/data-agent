"""数据源管理 API 的 Pydantic 模型。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.datasources.models import DataSourceKind


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    kind: DataSourceKind
    path: str | None = Field(default=None, max_length=1024)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = Field(default=None, max_length=256)
    schema_name: str | None = Field(default=None, alias="schema", max_length=256)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=4096)
    ssl_mode: Literal["disable", "prefer", "require", "verify-ca", "verify-full"] = "require"

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def validate_kind_fields(self):
        remote_values = (self.host, self.port, self.database, self.schema_name, self.username, self.password)
        if self.kind == DataSourceKind.SQLITE:
            if not self.path:
                raise ValueError("SQLite 数据源必须填写 path")
            if any(value is not None for value in remote_values):
                raise ValueError("SQLite 数据源不能携带远程连接字段")
        elif self.path is not None:
            raise ValueError("PostgreSQL/MySQL 数据源不能携带 path")
        elif not all((self.host, self.database, self.username, self.password)):
            raise ValueError("远程数据源必须填写 host、database、username 和 password")
        if self.kind == DataSourceKind.MYSQL and self.ssl_mode not in {"disable", "require"}:
            raise ValueError("MySQL ssl_mode 仅支持 disable 或 require")
        if self.kind == DataSourceKind.MYSQL and self.schema_name not in {None, "", self.database}:
            raise ValueError("MySQL 的 database 即 schema，不能填写不同的 schema")
        return self


class SemanticDraftRequest(BaseModel):
    table_ids: list[int] | None = Field(default=None, min_length=1, max_length=100)

    model_config = ConfigDict(extra="forbid")

    @field_validator("table_ids")
    @classmethod
    def unique_table_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("table_ids 不能重复")
        return value


class ColumnReview(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    comment: str | None = Field(default=None, max_length=1000)
    synonyms: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list, max_length=20)

    model_config = ConfigDict(extra="forbid")


class TableReviewRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    table_comment: str | None = Field(default=None, max_length=1000)
    columns: list[ColumnReview] = Field(default_factory=list, max_length=500)

    model_config = ConfigDict(extra="forbid")

    @field_validator("columns")
    @classmethod
    def unique_columns(cls, value: list[ColumnReview]) -> list[ColumnReview]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("columns 不能包含重复字段")
        return value
