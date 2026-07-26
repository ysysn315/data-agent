"""持久化层（SQLAlchemy 2.0 async）

D 轮落地：把此前「内存 + JSON 落盘」的存储实现换成数据库，上层 API 不变。
SQLite 起步、PostgreSQL 就绪（只改 settings.database_url 一行）。

模块划分：
- models.py：Declarative 四张表（skills / mcp_servers / sql_examples / terminology）
- engine.py：async engine / session 单例、建表、sync→async 桥接
- repositories.py：async 数据访问层（技能仓储对齐 InMemorySkillRepository 契约）

设计说明见 app/db/IMPLEMENTATION.md。
"""
from __future__ import annotations

from app.db.engine import (
    create_engine_and_sessionmaker,
    ensure_initialized,
    get_engine,
    get_sessionmaker,
    init_db,
    reset_engine,
    run_sync,
)

__all__ = [
    "create_engine_and_sessionmaker",
    "ensure_initialized",
    "get_engine",
    "get_sessionmaker",
    "init_db",
    "reset_engine",
    "run_sync",
]
