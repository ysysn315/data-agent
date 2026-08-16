"""请求级图谱作用域。

图谱工具不暴露租户参数，API/Chat 路由在进入 Agent 前设置 ContextVar。ContextVar
天然按协程隔离，适合 FastAPI 并发请求；GraphStore 仍把 scope 显式传给仓储，避免
把可变租户状态放进单例服务。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class GraphScope:
    """图谱作用域。datasource_id=None 表示 workspace 级图谱。"""

    workspace_id: int = 0
    datasource_id: int | None = None

    @property
    def scope_type(self) -> str:
        return "datasource" if self.datasource_id is not None else "workspace"

    @property
    def key(self) -> str:
        return (
            f"datasource:{self.datasource_id}" if self.datasource_id is not None else f"workspace:{self.workspace_id}"
        )

    @classmethod
    def from_ids(cls, workspace_id: int | None, datasource_id: int | None = None) -> "GraphScope":
        return cls(workspace_id=workspace_id or 0, datasource_id=datasource_id)


_scope: ContextVar[GraphScope | None] = ContextVar("graph_scope", default=None)


def current_graph_scope() -> GraphScope | None:
    return _scope.get()


@contextmanager
def use_graph_scope(scope: GraphScope) -> Iterator[None]:
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)
