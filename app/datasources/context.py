"""聊天请求级数据源选择。

工具签名不暴露 datasource_id，模型无法自行切换租户数据源；路由在进入 Agent 前设置，
请求结束后 reset。ContextVar 可隔离并发协程。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from app.graph.scope import GraphScope, use_graph_scope


@dataclass(frozen=True)
class DataSourceSelection:
    datasource_id: int
    workspace_id: int


_selection: ContextVar[DataSourceSelection | None] = ContextVar("datasource_selection", default=None)


def current_selection() -> DataSourceSelection | None:
    return _selection.get()


@contextmanager
def use_datasource(datasource_id: int | None, workspace_id: int) -> Iterator[None]:
    token = _selection.set(
        DataSourceSelection(datasource_id=datasource_id, workspace_id=workspace_id)
        if datasource_id is not None
        else None
    )
    try:
        yield
    finally:
        _selection.reset(token)


@contextmanager
def use_datasource_graph_scope(datasource_id: int | None, workspace_id: int) -> Iterator[None]:
    """同时设置数据源和图谱作用域，保证 Agent 链路使用同一租户上下文。"""

    with use_datasource(datasource_id, workspace_id), use_graph_scope(GraphScope.from_ids(workspace_id, datasource_id)):
        yield
