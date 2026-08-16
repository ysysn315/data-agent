"""持久化层 - async engine / session 管理 + sync→async 桥接

参考 Yuxi manager.py（PostgresManager）的 async engine + async_sessionmaker 思路，
按本项目 SQLite 起步的实际做了两点收敛：

1. **单引擎单会话工厂**（settings.database_url 驱动），NullPool。
   NullPool 每次操作现开现关连接，连接不跨操作复用，因此同一个 engine 既能被
   FastAPI 主事件循环用（技能仓储，天然 async），又能被后台事件循环用（下方 run_sync
   桥接的 MCP/示例/术语三个同步门面），不会踩 asyncio 连接绑定单循环的坑。

2. **run_sync 后台循环桥**：MCP/ExampleStore/TermStore 的公开方法是同步的
   （路由/工具直接 store.add(...) 调用），但底层 DB 是 async。若在 FastAPI 正在运行的
   主循环里 asyncio.run 会报 "loop already running"、submit 回主循环又会自死锁。
   故起一个**独立守护线程 + 独立事件循环**承接这些同步门面的协程，
   run_coroutine_threadsafe 提交并阻塞取结果——跨循环所以不死锁。
   （技能仓储是纯 async、由主循环 await，不走这条桥。）

alembic 迁移留二期：起步只有 create_all 幂等建表，schema 尚在快速变动，
迁移脚本的维护成本大于收益；理由详见 IMPLEMENTATION.md。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Coroutine, Optional, TypeVar

from loguru import logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.settings import settings
from app.db.models import Base

T = TypeVar("T")


def _ensure_sqlite_dir(database_url: str) -> None:
    """SQLite 文件库：确保父目录存在（首次启动 ./data 可能还没建）。"""
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return
    raw = database_url[len(prefix) :]
    if not raw or raw == ":memory:":
        return
    db_path = Path(raw)
    db_path.parent.mkdir(parents=True, exist_ok=True)


def create_engine_and_sessionmaker(
    database_url: str,
    echo: bool = False,
) -> tuple[AsyncEngine, async_sessionmaker]:
    """按 URL 造一对 (engine, sessionmaker)。测试每个用例用独立 sqlite 文件时直接调它。

    NullPool：连接不跨操作复用，规避 async 连接绑定单事件循环的问题。
    SQLite 额外设 busy_timeout，避免主循环/后台循环并发访问偶发 "database is locked"。
    """
    _ensure_sqlite_dir(database_url)
    engine = create_async_engine(database_url, echo=echo, poolclass=NullPool)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _connection_record):  # pragma: no cover - 连接钩子
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sessionmaker


async def init_db(engine: AsyncEngine) -> None:
    """先升级旧图谱/知识库表，再幂等建表。测试与运行时共用同一入口。"""
    async with engine.begin() as conn:
        from app.db.graph_migration import upgrade_graph_schema
        from app.db.knowledge_migration import upgrade_knowledge_schema

        await conn.run_sync(upgrade_graph_schema)
        await conn.run_sync(upgrade_knowledge_schema)
        await conn.run_sync(Base.metadata.create_all)


# ========== 运行时单例：engine / sessionmaker ==========

_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker] = None
_engine_lock = threading.Lock()


def get_engine() -> AsyncEngine:
    """全局 engine 单例（settings.database_url）。切 PG 只改 database_url。"""
    global _engine, _sessionmaker
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine, _sessionmaker = create_engine_and_sessionmaker(settings.database_url)
                logger.info(f"初始化持久化 engine: {settings.database_url}")
    return _engine


def get_sessionmaker() -> async_sessionmaker:
    """全局 async_sessionmaker 单例。"""
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


# ========== 后台事件循环（承接同步门面的 async DB 调用） ==========


class _BackgroundLoop:
    """独立守护线程 + 事件循环，用于 sync→async 桥接。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="db-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """在后台循环里跑协程并阻塞取结果（跨循环，故不与主循环死锁）。"""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


_bg_loop: Optional[_BackgroundLoop] = None
_bg_lock = threading.Lock()


def _get_bg_loop() -> _BackgroundLoop:
    global _bg_loop
    if _bg_loop is None:
        with _bg_lock:
            if _bg_loop is None:
                _bg_loop = _BackgroundLoop()
    return _bg_loop


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """同步门面（MCPService / ExampleStore / TermStore 的 DB 版）用它跑 async DB 操作。"""
    return _get_bg_loop().run(coro)


# ========== 建表初始化（幂等、一次性） ==========

_initialized = False
_init_lock = threading.Lock()


def ensure_initialized() -> None:
    """确保表已建好；幂等、可从任意线程调用（在后台循环里执行 create_all）。

    技能仓储（主循环 async）与三个同步门面（后台循环）在首次使用前都调它，
    保证无论谁先访问 DB，表都已存在。
    """
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        run_sync(init_db(get_engine()))
        _initialized = True
        logger.info("持久化建表完成（create_all）")


def reset_engine() -> None:
    """重置 engine / sessionmaker / 建表标记（测试或运行时重配用）。

    不销毁后台循环线程（守护线程、复用即可）；engine 用 NullPool 无常驻连接，
    直接丢弃引用即可，无需 await dispose。
    """
    global _engine, _sessionmaker, _initialized
    _engine = None
    _sessionmaker = None
    _initialized = False
