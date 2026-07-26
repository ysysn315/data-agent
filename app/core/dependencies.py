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
    """SQL 示例库单例（持久化：save_dir/sql_examples.json）"""
    global _example_store
    if _example_store is None:
        from app.text2sql.examples import ExampleStore

        _example_store = ExampleStore(Path(settings.save_dir) / "sql_examples.json")
    return _example_store


def get_term_store():
    """业务术语库单例（持久化：save_dir/terminology.json）"""
    global _term_store
    if _term_store is None:
        from app.text2sql.terminology import TermStore

        _term_store = TermStore(Path(settings.save_dir) / "terminology.json")
    return _term_store


async def get_skill_service():
    """SkillService 单例（启动时加载内置 Skills）"""
    global _skill_service
    if _skill_service is not None:
        return _skill_service

    async with _init_lock:
        if _skill_service is not None:
            return _skill_service

        from app.skills.repository import InMemorySkillRepository
        from app.skills.service import SkillService

        service = SkillService(
            repository=InMemorySkillRepository(),
            save_dir=settings.save_dir,
        )

        builtin_dir = Path(__file__).parent.parent / "skills" / "buildin"
        await service.load_builtin_skills(builtin_dir)

        _skill_service = service
    return _skill_service


def get_mcp_service():
    """MCPService 单例（注册表：save_dir/mcp_servers.json）"""
    global _mcp_service
    if _mcp_service is None:
        from app.mcp.service import MCPService

        _mcp_service = MCPService(
            config_path=Path(settings.save_dir) / "mcp_servers.json"
        )
    return _mcp_service


async def get_chat_agent():
    """ChatAgent 单例（LLMFactory + Skills/MCP/工具熔断中间件）"""
    global _chat_agent
    if _chat_agent is not None:
        return _chat_agent

    async with _init_lock:
        if _chat_agent is not None:
            return _chat_agent

        from app.agents.chat_agent import ChatAgent
        from app.agents.middlewares import ToolRuntimeMiddleware
        from app.agents.tools.datetime_tool import get_current_datetime
        from app.agents.tools.schema_tool import create_schema_search_tool
        from app.agents.tools.sql_context_tool import create_sql_context_tool
        from app.agents.tools.sql_tool import create_execute_sql_tool
        from app.core.llm import LLMFactory
        from app.skills.middleware import SkillsMiddleware
        from app.skills.tools import create_skill_tools

        skill_service = await get_skill_service()
        mcp_service = get_mcp_service()

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
    """重置单例（测试用）"""
    global _skill_service, _mcp_service, _chat_agent
    _skill_service = None
    _mcp_service = None
    _chat_agent = None
