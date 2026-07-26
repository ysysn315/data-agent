"""Langfuse 调用链追踪接入（默认关闭、缺配置零影响主流程）。

三条约束，对应任务的"零侵入"要求：
- 默认关闭：``langfuse_enabled`` 为 False，或 public/secret key 为空时直接返回 []，
  连 langfuse 都不 import —— 未安装 / 未配置时没有任何开销。
- 惰性单例：仅在启用时才 import 并构造一次 CallbackHandler，进程内复用。
- 失败降级：初始化 Langfuse 客户端或 CallbackHandler 抛错时，记 warning 并返回 []，
  绝不让 tracing 故障波及对话主链路。
"""
from __future__ import annotations

from typing import Any, List

from loguru import logger

from app.core.settings import settings

# 进程内单例：仅缓存"已启用"分支的构造结果（成功为 [handler]，失败降级为 []）。
# 未启用分支不写缓存，始终走零开销早返回。
_callbacks_cache: List[Any] | None = None


def get_langfuse_callbacks() -> List[Any]:
    """返回注入 LangGraph ``config={"callbacks": ...}`` 的 callbacks 列表。

    未启用或缺 key 时返回空列表 —— 此时 Agent 行为与未接入 tracing 完全一致。
    """
    if not (settings.langfuse_enabled and settings.langfuse_public_key and settings.langfuse_secret_key):
        # 未启用 / 缺 key：不 import langfuse，零开销
        return []

    global _callbacks_cache
    if _callbacks_cache is None:
        _callbacks_cache = _build_callbacks()
    return _callbacks_cache


def _build_callbacks() -> List[Any]:
    """惰性构造 Langfuse CallbackHandler；任何异常都降级为空列表。"""
    try:
        # langfuse v3/v4 的 langchain 集成路径（v2 是 langfuse.callback.CallbackHandler，
        # 本项目装的是 v4，凭证由 Langfuse(...) 客户端注入，CallbackHandler() 无参复用之）。
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        handler = CallbackHandler()
        logger.info(f"Langfuse 调用链追踪已启用（host={settings.langfuse_host}）")
        return [handler]
    except Exception as exc:  # 追踪挂了不能影响对话
        logger.warning(f"初始化 Langfuse 追踪失败，将跳过 tracing（不影响对话）: {exc}")
        return []


def reset_langfuse_callbacks() -> None:
    """清空单例缓存。仅供测试在 monkeypatch settings 后复位使用。"""
    global _callbacks_cache
    _callbacks_cache = None
