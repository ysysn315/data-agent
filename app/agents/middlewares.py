"""Agent 通用中间件

ToolRuntimeMiddleware：把 tool_runtime 的重试/超时/熔断/降级能力
以 langchain v1 AgentMiddleware 的形式接入 create_agent 工具执行链。
（原 my-agent 在手写 StateGraph 里直接调 safe_tool_execute，v2 改为中间件挂载）
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from loguru import logger

from app.agents.tool_runtime import safe_tool_execute


class ToolRuntimeMiddleware(AgentMiddleware):
    """工具执行保护：按 TOOL_POLICIES 对每个工具调用做重试 + 超时 + 熔断。

    失败时不抛异常，而是把 TOOL_FALLBACK_MESSAGES 里的降级文案作为
    ToolMessage 返回给模型，让 Agent 降级续跑而不是崩溃。
    """

    async def awrap_tool_call(self, request, handler):
        tool_name = request.tool_call["name"]
        tool_call_id = request.tool_call["id"]

        # 用 cell 保存 handler 的真实返回（ToolMessage/Command），
        # 给 tool_runtime 的是其文本形式（用于失败启发式判断）
        result_cell: dict = {}

        async def invoke():
            result = await handler(request)
            result_cell["value"] = result
            content = getattr(result, "content", None)
            return str(content) if content is not None else str(result)

        execution = await safe_tool_execute(tool_name, invoke, {})

        if execution.success and "value" in result_cell:
            return result_cell["value"]

        # 降级：熔断打开 / 重试耗尽，返回降级文案
        logger.warning(
            f"工具 {tool_name} 降级返回: status={execution.status}, "
            f"attempts={execution.attempts}, error={execution.error}"
        )
        return ToolMessage(
            content=f"[tool_status={execution.status}] {execution.content}",
            tool_call_id=tool_call_id,
            status="error",
        )
