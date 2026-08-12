"""请求级 RAG 参数。

顶层 ChatRequest 的过滤条件不放进工具签名的必填参数里，而是由路由进入
Agent 前写入 ContextVar。这样模型仍可在一次对话中主动指定工具过滤条件，
同时 API 调用方传入的条件也不会在 ChatAgent 的多轮工具调用间丢失。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_metadata_filters: ContextVar[dict | None] = ContextVar("rag_metadata_filters", default=None)
_sources: ContextVar[list[str] | None] = ContextVar("rag_sources", default=None)


def current_metadata_filters() -> dict:
    """返回当前请求的过滤条件副本，避免工具调用修改上下文对象。"""
    return dict(_metadata_filters.get() or {})


def record_sources(sources: list[str]) -> None:
    """记录本次请求实际返回给模型的文档来源。"""
    current = _sources.get()
    if current is None:
        return
    for source in sources:
        value = str(source or "").strip()
        if value and value not in current:
            current.append(value)


def current_sources() -> list[str]:
    return list(_sources.get() or [])


@contextmanager
def use_metadata_filters(filters: dict | None) -> Iterator[None]:
    filter_token = _metadata_filters.set(dict(filters or {}) or None)
    source_token = _sources.set([])
    try:
        yield
    finally:
        _sources.reset(source_token)
        _metadata_filters.reset(filter_token)
