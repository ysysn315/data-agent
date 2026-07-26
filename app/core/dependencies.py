"""FastAPI 依赖注入

单例：SkillService / MCPService / ChatAgent。
所有路由必须从这里取实例 —— 不要在路由文件里自定义同名依赖
（曾因 routes_skills 本地遮蔽 get_skill_service 导致 API 返回空列表）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException, status

from app.core.settings import settings


async def get_current_user(
    authorization: str = Header(..., description="Bearer token")
) -> dict:
    """获取当前用户（必须登录）"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format"
        )

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is empty"
        )

    # TODO: 二期实现真实 token 验证
    return {"id": 1, "username": "dev_user", "token": token}


async def get_current_user_optional(
    authorization: str = Header(None, description="Bearer token")
) -> Optional[dict]:
    """获取当前用户（可选登录）"""
    if not authorization or not authorization.startswith("Bearer "):
        return None

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None

    return {"id": 1, "username": "dev_user", "token": token}


# ========== 单例 ==========

_skill_service = None
_mcp_service = None
_chat_agent = None
_example_store = None
_term_store = None
_init_lock = asyncio.Lock()


def get_example_store():
    """SQL 示例库单例（DB 版；save_dir/sql_examples.json 作历史 JSON 迁移源）"""
    global _example_store
    if _example_store is None:
        from app.db import ensure_initialized, get_sessionmaker, run_sync
        from app.db.repositories import SQLExampleRepository
        from app.text2sql.examples import ExampleStore

        ensure_initialized()
        _example_store = ExampleStore(
            Path(settings.save_dir) / "sql_examples.json",
            repo=SQLExampleRepository(get_sessionmaker()),
            runner=run_sync,
        )
    return _example_store


def get_term_store():
    """业务术语库单例（DB 版；save_dir/terminology.json 作历史 JSON 迁移源）"""
    global _term_store
    if _term_store is None:
        from app.db import ensure_initialized, get_sessionmaker, run_sync
        from app.db.repositories import TerminologyRepository
        from app.text2sql.terminology import TermStore

        ensure_initialized()
        _term_store = TermStore(
            Path(settings.save_dir) / "terminology.json",
            repo=TerminologyRepository(get_sessionmaker()),
            runner=run_sync,
        )
    return _term_store


async def get_skill_service():
    """SkillService 单例（DB 版仓储；启动时从文件系统加载内置 Skills 进缓存）"""
    global _skill_service
    if _skill_service is not None:
        return _skill_service

    async with _init_lock:
        if _skill_service is not None:
            return _skill_service

        from app.db import ensure_initialized, get_sessionmaker
        from app.db.repositories import SqlAlchemySkillRepository
        from app.skills.service import SkillService

        ensure_initialized()
        service = SkillService(
            repository=SqlAlchemySkillRepository(get_sessionmaker()),
            save_dir=settings.save_dir,
        )

        builtin_dir = Path(__file__).parent.parent / "skills" / "buildin"
        await service.load_builtin_skills(builtin_dir)

        _skill_service = service
    return _skill_service


def get_mcp_service():
    """MCPService 单例（DB 版；save_dir/mcp_servers.json 作历史 JSON 迁移源）"""
    global _mcp_service
    if _mcp_service is None:
        from app.db import ensure_initialized, get_sessionmaker, run_sync
        from app.db.repositories import MCPRepository
        from app.mcp.service import MCPService

        ensure_initialized()
        _mcp_service = MCPService(
            config_path=Path(settings.save_dir) / "mcp_servers.json",
            repo=MCPRepository(get_sessionmaker()),
            runner=run_sync,
        )
    return _mcp_service


async def get_chat_agent():
    """ChatAgent 单例（LLMFactory + Skills/MCP/工具熔断中间件）"""
    global _chat_agent
    if _chat_agent is not None:
        return _chat_agent

    # 必须在拿 _init_lock 之前完成：get_skill_service 内部要拿同一把锁，
    # asyncio.Lock 不可重入，放在锁内调用会死锁（曾导致首次 chat 永久挂起）
    skill_service = await get_skill_service()
    mcp_service = get_mcp_service()

    async with _init_lock:
        if _chat_agent is not None:
            return _chat_agent

        from app.agents.chat_agent import ChatAgent
        from app.agents.middlewares import ToolRuntimeMiddleware
        from app.agents.tools.datetime_tool import get_current_datetime
        from app.agents.tools.schema_tool import create_schema_search_tool
        from app.agents.tools.graph_tool import create_graph_search_tool
        from app.agents.tools.sql_context_tool import create_sql_context_tool
        from app.agents.tools.sql_tool import create_execute_sql_tool
        from app.core.llm import LLMFactory
        from app.skills.middleware import SkillsMiddleware
        from app.skills.tools import create_skill_tools

        base_tools = [get_current_datetime]
        base_tools.extend(create_skill_tools(skill_service))

        if settings.tavily_api_key:
            from app.agents.tools.tavily_tool import create_tavily_search_tool
            base_tools.append(
                create_tavily_search_tool(settings.tavily_api_key, settings.tavily_base_url)
            )

        if settings.enable_kb_tool:
            # 知识库检索依赖 Milvus。连不上就显式失败：
            # 未部署 Milvus 时请设置 ENABLE_KB_TOOL=false
            from app.agents.tools.internal_docs_tool import create_docs_tool
            from app.clients.milvus_client import MilvusClient
            from app.rag.embeddings import EmbeddingService
            from app.rag.vector_store import VectorStore

            milvus_client = MilvusClient(settings)
            await milvus_client.connect()
            await milvus_client.ensure_collection()
            vector_store = VectorStore(milvus_client, EmbeddingService(settings))
            base_tools.append(create_docs_tool(vector_store))

        # 技能声明的门控工具：构建期注册，read_skill 激活后才对模型可见
        gated_tools = [
            create_execute_sql_tool(settings.sqlite_db_path),
            create_schema_search_tool(settings.sqlite_db_path),
            create_sql_context_tool(get_example_store(), get_term_store()),
            create_graph_search_tool(get_graph_service()),
        ]

        _chat_agent = ChatAgent(
            llm=LLMFactory.create_llm(),
            tools=base_tools,
            middleware=[
                SkillsMiddleware(
                    skill_service=skill_service,
                    mcp_service=mcp_service,
                    gated_tools=gated_tools,
                ),
                ToolRuntimeMiddleware(),
            ],
        )
    return _chat_agent


def reset_singletons() -> None:
    """重置单例（测试用）

    只清业务单例引用；持久化 engine/后台循环是进程级、守护线程常驻，
    NullPool 无常驻连接，不随单例重置销毁（如需换库调 app.db.reset_engine）。
    """
    global _skill_service, _mcp_service, _chat_agent, _example_store, _term_store, _task_service, _graph_service
    _skill_service = None
    _mcp_service = None
    _chat_agent = None
    _example_store = None
    _term_store = None
    _task_service = None
    _graph_service = None


# ========== 异步任务（arq + Redis Streams），D 轮追加 ==========

_task_service = None


async def get_task_service():
    """TaskService 单例（异步任务入队 + 事件流）。

    元数据/事件流用 decode_responses=True 的异步 Redis；入队用 arq 连接池。
    连接惰性建立在首次请求（导入本模块不触发 Redis 连接）；测试通过
    dependency_overrides 注入 fakeredis 版本，不走这里。
    """
    global _task_service
    if _task_service is not None:
        return _task_service

    async with _init_lock:
        if _task_service is not None:
            return _task_service

        from app.tasks.service import TaskService, create_arq_pool, create_task_redis

        _task_service = TaskService(
            redis=create_task_redis(settings),
            arq_pool=await create_arq_pool(settings),
        )
    return _task_service


# ========== 知识图谱（E 轮追加） ==========

_graph_service = None


def get_graph_service():
    """GraphService 单例（graph_triples 表 + NetworkX 内存镜像；首启表空写入演示种子）。

    LLM 惰性注入：llm_provider 传工厂而非实例，不调抽取接口就不要求配置 LLM_API_KEY。
    注意：reset_singletons 早于本段存在、未覆盖 _graph_service（本文件权限边界为
    只在末尾追加）；测试请用 dependency_overrides 注入独立实例，不依赖单例重置。
    """
    global _graph_service
    if _graph_service is None:
        from app.core.llm import LLMFactory
        from app.db import ensure_initialized, get_sessionmaker, run_sync
        from app.db.repositories import GraphTripleRepository
        from app.graph.service import GraphService
        from app.graph.store import GraphStore

        ensure_initialized()
        _graph_service = GraphService(
            store=GraphStore(GraphTripleRepository(get_sessionmaker()), runner=run_sync),
            llm_provider=LLMFactory.create_llm,
        )
    return _graph_service
