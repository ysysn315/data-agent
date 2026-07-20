"""Skills 系统 - LangGraph Middleware 注入"""
from __future__ import annotations

from typing import Any, Optional

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from loguru import logger

from app.skills.models import ExpandedSkills
from app.skills.service import SkillService


class SkillsMiddleware(AgentMiddleware):
    """
    Skills 中间件：将 Skills 提示词和工具注入 Agent

    使用方式：
        agent = ChatAgent(
            llm=llm,
            tools=tools,
            middlewares=[
                SkillsMiddleware(
                    skill_service=skill_service,
                    enabled_skills=["schema-retrieval", "sql-generation"],
                    auto_match=True  # 自动根据用户输入匹配 skills
                )
            ]
        )
    """

    def __init__(
        self,
        skill_service: SkillService,
        enabled_skills: Optional[list[str]] = None,
        auto_match: bool = False,
        auto_expand_dependencies: bool = True,
        max_match_skills: int = 3
    ):
        """
        Args:
            skill_service: Skills 业务逻辑实例
            enabled_skills: 启用的 skill slug 列表（None=自动匹配）
            auto_match: 是否根据用户输入自动匹配 skills
            auto_expand_dependencies: 是否自动展开依赖
            max_match_skills: 自动匹配时最多挂载的 skills 数
        """
        self.skill_service = skill_service
        self.enabled_skills = enabled_skills or []
        self.auto_match = auto_match
        self.auto_expand_dependencies = auto_expand_dependencies
        self.max_match_skills = max_match_skills

        # 缓存展开结果（避免每次请求都重新展开）
        self._expanded_cache: Optional[ExpandedSkills] = None
        self._cache_key: Optional[str] = None

    async def _get_expanded_skills(
        self,
        user_input: str = ""
    ) -> ExpandedSkills:
        """
        获取展开后的 skills

        策略：
        1. 如果指定了 enabled_skills，直接展开
        2. 如果 auto_match=True，根据 user_input 匹配 skills 再展开
        3. 否则返回空
        """
        # 确定要展开的 skill slugs
        if self.enabled_skills:
            slugs = self.enabled_skills
        elif self.auto_match and user_input:
            matched = await self.skill_service.match_skills_by_query(
                query=user_input,
                top_k=self.max_match_skills
            )
            slugs = [skill.slug for skill in matched]
        else:
            return ExpandedSkills()

        # 缓存键
        cache_key = "|".join(sorted(slugs))
        if self._expanded_cache and self._cache_key == cache_key:
            return self._expanded_cache

        # 展开依赖
        if self.auto_expand_dependencies:
            expanded = await self.skill_service.expand_dependencies(slugs)
        else:
            # 不展开依赖，只加载指定 skills
            expanded = ExpandedSkills()
            for slug in slugs:
                skill = await self.skill_service.get_skill(slug)
                if skill and skill.enabled:
                    expanded.add_skill(skill)
            expanded.deduplicate()

        # 缓存
        self._expanded_cache = expanded
        self._cache_key = cache_key

        return expanded

    async def modify_model_request(
        self,
        request: ModelRequest,
        **kwargs: Any
    ) -> ModelRequest:
        """
        修改模型请求，注入 Skills 提示词和工具

        这是 LangGraph AgentMiddleware 的标准接口
        """
        # 提取用户输入
        user_input = ""
        if request.messages:
            last_message = request.messages[-1]
            if hasattr(last_message, "content"):
                user_input = str(last_message.content)

        # 获取展开的 skills
        expanded = await self._get_expanded_skills(user_input)

        if not expanded.skills:
            return request

        # 1. 注入 system message（Skills 提示词）
        skills_prompt = expanded.build_system_prompt()
        if skills_prompt:
            # 追加到现有 system message
            existing_system = ""
            if request.system_message:
                existing_system = str(request.system_message.content)

            new_system = f"{existing_system}\n\n{skills_prompt}".strip()

            # 创建新的 system message
            from langchain_core.messages import SystemMessage
            request.system_message = SystemMessage(content=new_system)

        # 2. 注入工具（skills 依赖的工具）
        if expanded.tools:
            # 注意：这里只是标记需要哪些工具
            # 实际工具实例由 Agent 在初始化时提供
            # 我们可以在 request 的上下文中记录，供后续使用
            if not hasattr(request, "context"):
                request.context = {}
            request.context["required_tools"] = expanded.tools

        # 3. 注入 MCP（skills 依赖的 MCP servers）
        if expanded.mcps:
            if not hasattr(request, "context"):
                request.context = {}
            request.context["required_mcps"] = expanded.mcps

        logger.info(
            f"Skills 注入: {len(expanded.skills)} skills, "
            f"{len(expanded.tools)} tools, {len(expanded.mcps)} mcps"
        )

        return request

    async def modify_model_response(
        self,
        response: ModelResponse,
        **kwargs: Any
    ) -> ModelResponse:
        """
        修改模型响应（当前不处理，二期可用于结果后处理）
        """
        return response


class SkillsToolFilter:
    """
    Skills 工具过滤器：根据 skills 依赖过滤可用工具

    用于 Agent 执行时，只提供 skills 声明的工具
    """

    def __init__(self, skill_service: SkillService):
        self.skill_service = skill_service

    async def filter_tools(
        self,
        available_tools: dict[str, Any],
        skill_slugs: list[str]
    ) -> dict[str, Any]:
        """
        根据 skills 依赖过滤工具

        Args:
            available_tools: 所有可用工具 {name: tool_instance}
            skill_slugs: 当前挂载的 skill slug 列表

        Returns:
            过滤后的工具字典
        """
        expanded = await self.skill_service.expand_dependencies(skill_slugs)
        required_tools = set(expanded.tools)

        # 过滤：只保留 skills 声明的工具
        filtered = {
            name: tool
            for name, tool in available_tools.items()
            if name in required_tools
        }

        logger.info(
            f"工具过滤: 总工具 {len(available_tools)}, "
            f"skills 需要 {len(required_tools)}, "
            f"实际提供 {len(filtered)}"
        )

        return filtered