"""Skills 系统 - Agent 中间件（langchain v1 AgentMiddleware）

对齐 Yuxi 的三段式设计：
1. 渐进式披露：system prompt 只注入技能名称+描述，正文由模型调用 read_skill 按需读取
2. 激活门控：拦截 read_skill 的调用结果 → 把 slug 写入 state.activated_skills →
   该技能声明的工具在下一次模型调用时解锁
3. MCP 懒加载：技能激活后才加载其声明的 MCP server 工具，
   动态工具通过 wrap_tool_call 的 request.override(tool=...) 接管执行
   （langchain v1 不允许执行构建期未注册的工具）

本地门控工具（如 execute_sql）挂在 middleware.tools 上 —— 构建期注册进 ToolNode
（否则执行时报 "not a valid tool"，Yuxi skills.py:151-164 同款坑），请求期按激活状态过滤可见性。
"""
from __future__ import annotations

from typing import Annotated, NotRequired, Optional, Sequence

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from loguru import logger

from app.skills.service import SkillService

READ_SKILL_TOOL_NAME = "read_skill"


def _activated_skills_reducer(left: Optional[list[str]], right: Optional[list[str]]) -> list[str]:
    """保序去重合并已激活技能列表"""
    result: list[str] = []
    for slug in (left or []) + (right or []):
        if slug not in result:
            result.append(slug)
    return result


class SkillsState(AgentState):
    """扩展 Agent 状态：记录已激活的技能"""
    activated_skills: NotRequired[Annotated[list[str], _activated_skills_reducer]]


class SkillsMiddleware(AgentMiddleware):
    """Skills 中间件：渐进式披露 + 激活门控 + MCP 懒加载"""

    state_schema = SkillsState

    def __init__(
        self,
        skill_service: SkillService,
        mcp_service=None,
        enabled_skills: Optional[list[str]] = None,
        gated_tools: Sequence[BaseTool] = (),
        auto_match: bool = False,
        max_match_skills: int = 3,
    ):
        """
        Args:
            skill_service: Skills 业务逻辑实例
            mcp_service: MCP 服务（None=不加载 MCP 工具）
            enabled_skills: 挂载的 skill slug 列表（None=全部启用的 skills）
            gated_tools: 技能声明的本地工具实例（构建期注册，激活前对模型隐藏）
            auto_match: 按用户输入自动匹配 skills（替代全量挂载）
            max_match_skills: 自动匹配时最多挂载数
        """
        super().__init__()
        self.skill_service = skill_service
        self.mcp_service = mcp_service
        self.enabled_skills = enabled_skills
        self.auto_match = auto_match
        self.max_match_skills = max_match_skills

        # AgentMiddleware.tools：create_agent 构建期自动注册进 ToolNode
        self.tools = list(gated_tools)
        self._gated_tool_names = {t.name for t in gated_tools}

        # 动态 MCP 工具注册表：name -> tool 实例（wrap_tool_call 用其接管执行）
        self._mcp_tools: dict[str, BaseTool] = {}

    # ========== 披露 + 门控（模型调用前） ==========

    async def _resolve_root_slugs(self, request: ModelRequest) -> list[str]:
        """确定本次请求挂载哪些 skills"""
        if self.enabled_skills is not None:
            return self.enabled_skills

        if self.auto_match:
            user_input = ""
            for msg in reversed(request.messages):
                if isinstance(msg, HumanMessage):
                    user_input = str(msg.content)
                    break
            if user_input:
                matched = await self.skill_service.match_skills_by_query(
                    query=user_input, top_k=self.max_match_skills
                )
                return [s.slug for s in matched]
            return []

        # 默认：全部启用的 skills（对齐 Yuxi context.skills=None 语义）
        skills = await self.skill_service.list_skills(enabled_only=True)
        return [s.slug for s in skills]

    async def awrap_model_call(self, request: ModelRequest, handler):
        expanded = await self.skill_service.expand_dependencies(
            await self._resolve_root_slugs(request)
        )

        if not expanded.skills:
            return await handler(request)

        activated = set(request.state.get("activated_skills", []))
        # 激活状态只认本次挂载闭包内的技能
        activated &= {s.slug for s in expanded.skills}

        # 1. 注入渐进式披露的技能列表
        skills_prompt = expanded.build_system_prompt()
        existing = ""
        if request.system_message is not None:
            existing = str(request.system_message.content)
        new_system = SystemMessage(content=f"{existing}\n\n{skills_prompt}".strip())

        # 2. 工具门控：未激活技能声明的本地工具从模型视野中隐藏
        declared = set(expanded.tools)
        unlocked = expanded.tools_of(activated)
        hidden = (declared & self._gated_tool_names) - unlocked
        model_tools = [t for t in request.tools if getattr(t, "name", None) not in hidden]

        # 3. MCP 懒加载：已激活技能声明的 MCP server 工具，加载并追加
        if self.mcp_service and activated:
            mcp_slugs = expanded.mcps_of(activated)
            if mcp_slugs:
                mcp_tools = await self.mcp_service.load_tools(mcp_slugs)
                existing_names = {getattr(t, "name", None) for t in model_tools}
                for mcp_tool in mcp_tools:
                    self._mcp_tools[mcp_tool.name] = mcp_tool
                    if mcp_tool.name not in existing_names:
                        model_tools.append(mcp_tool)

        logger.debug(
            f"Skills 注入: {len(expanded.skills)} skills, "
            f"已激活 {sorted(activated)}, 隐藏工具 {sorted(hidden)}"
        )
        return await handler(
            request.override(system_message=new_system, tools=model_tools)
        )

    # ========== 激活拦截 + MCP 动态工具执行（工具调用时） ==========

    def _maybe_activate(self, request, result):
        """read_skill 调用成功 → 把 slug 合并进 state.activated_skills"""
        if request.tool_call.get("name") != READ_SKILL_TOOL_NAME:
            return result

        slug = str(request.tool_call.get("args", {}).get("slug", "")).strip()
        if not slug:
            return result

        # 读取失败（技能不存在/未启用）不激活
        message = result
        if isinstance(result, Command):
            messages = (result.update or {}).get("messages", [])
            message = messages[-1] if messages else None
        if isinstance(message, ToolMessage):
            if str(message.content).startswith("技能不存在"):
                return result

        logger.info(f"技能已激活: {slug}")
        if isinstance(result, Command):
            update = dict(result.update or {})
            update["activated_skills"] = _activated_skills_reducer(
                update.get("activated_skills"), [slug]
            )
            return Command(update=update)
        return Command(update={"activated_skills": [slug], "messages": [result]})

    async def awrap_tool_call(self, request, handler):
        name = request.tool_call.get("name")
        # 动态 MCP 工具：构建期未注册，必须 override(tool=实例) 接管执行
        if name in self._mcp_tools:
            request = request.override(tool=self._mcp_tools[name])
        result = await handler(request)
        return self._maybe_activate(request, result)

    def wrap_tool_call(self, request, handler):
        name = request.tool_call.get("name")
        if name in self._mcp_tools:
            request = request.override(tool=self._mcp_tools[name])
        result = handler(request)
        return self._maybe_activate(request, result)
