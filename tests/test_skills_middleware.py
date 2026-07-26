"""SkillsMiddleware 端到端测试（假模型驱动真实 create_agent）

核心断言（对照 Yuxi test_skills_middleware 的关键行为）：
1. 渐进式披露：system prompt 只含名称+描述，不含正文
2. 门控：execute_sql 激活前对模型不可见，read_skill 后可见
3. read_skill 成功 → state.activated_skills 记录 slug
"""
from typing import Any, List

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.tools.sql_tool import create_execute_sql_tool
from app.skills.middleware import SkillsMiddleware
from app.skills.tools import create_skill_tools


class FakeToolCallingModel(BaseChatModel):
    """按脚本吐消息的假模型，记录每轮绑定的工具名"""
    scripted: List[AIMessage]
    step: int = 0
    seen_tool_names: List[List[str]] = []
    seen_system_prompts: List[str] = []

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        # 记录本轮 system prompt
        if messages and messages[0].type == "system":
            self.seen_system_prompts.append(str(messages[0].content))
        msg = self.scripted[min(self.step, len(self.scripted) - 1)]
        object.__setattr__(self, "step", self.step + 1)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs) -> "FakeToolCallingModel":
        self.seen_tool_names.append([getattr(t, "name", str(t)) for t in tools])
        return self


@pytest.fixture
def agent_parts(skill_service, demo_db):
    execute_sql = create_execute_sql_tool(demo_db)
    middleware = SkillsMiddleware(
        skill_service=skill_service,
        gated_tools=[execute_sql],
    )
    base_tools = create_skill_tools(skill_service)
    return middleware, base_tools


async def test_progressive_disclosure_and_gating(agent_parts):
    middleware, base_tools = agent_parts
    model = FakeToolCallingModel(
        scripted=[
            AIMessage(content="", tool_calls=[
                {"name": "read_skill", "args": {"slug": "sqlite-query"}, "id": "c1"},
            ]),
            AIMessage(content="", tool_calls=[
                {"name": "execute_sql",
                 "args": {"sql": "SELECT customer_state, SUM(price) FROM orders GROUP BY 1"},
                 "id": "c2"},
            ]),
            AIMessage(content="各州销售额：SP 130.0，RJ 50.5"),
        ],
        seen_tool_names=[],
        seen_system_prompts=[],
    )

    agent = create_agent(model=model, tools=base_tools, middleware=[middleware])
    result = await agent.ainvoke({"messages": [("user", "帮我看下各州销售额")]})

    # --- 披露：system prompt 只有名称+描述，不含正文 ---
    first_prompt = model.seen_system_prompts[0]
    assert "sqlite-query" in first_prompt
    assert "read_skill" in first_prompt
    assert "操作流程" not in first_prompt          # 正文标题不应出现
    assert "sqlite_master" not in first_prompt     # 正文细节不应出现

    # --- 门控：第一轮 execute_sql 不可见，激活后第二轮可见 ---
    assert "execute_sql" not in model.seen_tool_names[0]
    assert "read_skill" in model.seen_tool_names[0]
    assert "execute_sql" in model.seen_tool_names[1]

    # --- 激活状态入 state ---
    assert "sqlite-query" in result.get("activated_skills", [])

    # --- 工具真的执行了：SQL 结果在消息流里 ---
    all_text = " ".join(str(getattr(m, "content", "")) for m in result["messages"])
    assert "130" in all_text


async def test_read_nonexistent_skill_does_not_activate(agent_parts):
    middleware, base_tools = agent_parts
    model = FakeToolCallingModel(
        scripted=[
            AIMessage(content="", tool_calls=[
                {"name": "read_skill", "args": {"slug": "ghost-skill"}, "id": "c1"},
            ]),
            AIMessage(content="没有这个技能"),
        ],
        seen_tool_names=[],
        seen_system_prompts=[],
    )
    agent = create_agent(model=model, tools=base_tools, middleware=[middleware])
    result = await agent.ainvoke({"messages": [("user", "读一个不存在的技能")]})

    assert "ghost-skill" not in result.get("activated_skills", [])
    # 门控工具依然不可见
    for names in model.seen_tool_names:
        assert "execute_sql" not in names
