# 对话 Agent，支持工具调用。
# 使用 LangGraph 实现，避开 create_react_agent 的 StopIteration 问题。

import operator
from typing import Annotated, List, TypedDict

from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from loguru import logger

from app.agents.tool_runtime import safe_tool_execute


class AgentState(TypedDict):
    """Agent 状态定义。"""

    messages: Annotated[List, operator.add]


class ChatAgent:
    def __init__(self, api_key: str, model: str, tools: List):
        """
        初始化 ChatAgent。

        参数:
            api_key: DashScope API Key
            model: 模型名称，例如 "qwen-max"
            tools: 工具列表，例如 [get_current_datetime, query_internal_docs]
        """
        self.llm = ChatTongyi(
            dashscope_api_key=api_key,
            model_name=model,
            streaming=False,
        ).bind_tools(tools)

        self.tools = {tool.name: tool for tool in tools}
        self.graph = self._create_graph()

    def _create_graph(self):
        """创建 LangGraph 工作流。"""

        async def call_model(state: AgentState):
            """调用 LLM。"""

            messages = state["messages"]
            response = await self.llm.ainvoke(messages)
            return {"messages": [response]}

        async def call_tools(state: AgentState):
            """调用工具。"""

            last_message = state["messages"][-1]
            tool_calls = last_message.tool_calls

            results = []
            for tool_call in tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]

                tool = self.tools[tool_name]
                execution = await safe_tool_execute(tool_name, tool, tool_args)
                result = execution.content
                if not execution.success:
                    result = f"[tool_status={execution.status}] {result}"

                from langchain_core.messages import ToolMessage

                results.append(
                    ToolMessage(
                        content=str(result),
                        tool_call_id=tool_call["id"],
                    )
                )

            return {"messages": results}

        def should_continue(state: AgentState):
            """判断是否继续。"""

            last_message = state["messages"][-1]
            if hasattr(last_message, "tool_calls") and last_message.tool_calls:
                return "tools"
            return "end"

        workflow = StateGraph(AgentState)
        workflow.add_node("agent", call_model)
        workflow.add_node("tools", call_tools)
        workflow.set_entry_point("agent")
        workflow.add_conditional_edges(
            "agent",
            should_continue,
            {
                "tools": "tools",
                "end": END,
            },
        )
        workflow.add_edge("tools", "agent")

        return workflow.compile()

    async def chat(self, question: str, history: List[dict] = None, summary: str = "") -> str:
        """
        执行对话。

        参数:
            question: 用户问题
            history: 对话历史，可选

        返回:
            AI 回答
        """
        try:
            logger.info(f"ChatAgent 收到问题: {question}")

            messages = [
                SystemMessage(
                    content=(
                        "你是一个有帮助的助手。优先使用内部知识库，也可以使用其他工具；"
                        "当问题需要最新互联网信息，或者知识库中没有答案时，调用 `tavily_search`。"
                    )
                )
            ]

            if summary:
                messages.append(
                    SystemMessage(
                        content=f"以下是当前会话较早轮次的摘要，请在回答和工具决策时参考：\n{summary}"
                    )
                )

            if history:
                for msg in history:
                    if msg["role"] == "user":
                        messages.append(HumanMessage(content=msg["content"]))
                    else:
                        messages.append(AIMessage(content=msg["content"]))

            messages.append(HumanMessage(content=question))

            result = await self.graph.ainvoke({"messages": messages})

            logger.info(f"Agent 执行结果: {result}")

            if "messages" in result and len(result["messages"]) > 0:
                last_message = result["messages"][-1]
                answer = last_message.content if hasattr(last_message, "content") else str(last_message)
            else:
                logger.error(f"无法从结果中提取答案: {result}")
                answer = str(result)

            logger.info(f"ChatAgent 回答: {answer[:50]}...")
            return answer

        except Exception as e:  # noqa: BLE001
            logger.error(f"ChatAgent 执行失败: {str(e)}")
            import traceback

            logger.error(f"详细错误: {traceback.format_exc()}")
            raise
