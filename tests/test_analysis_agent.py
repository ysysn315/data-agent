"""AnalysisAgent（P-O-R 工作流）测试 —— 全离线，假模型，不调真 LLM。

覆盖：
1. Planner：合法 JSON 直接跑通 / 非法 JSON 重试一次成功 / 重试仍失败显式报错
2. 三步计划全流程 + 报告结构断言（概述 / 各步标题 / SQL 清单段）
3. Reflection 触发一次补充步骤，且第二次不再补充（防循环）
4. 进度事件序列：planning → step → reflecting → reporting → done
5. routes_analysis：TestClient + dependency_overrides 注入假 agent

假模型设计：
- FakeScriptedLLM：按脚本逐次吐 AIMessage，供 Planner + Reflection 的 llm.ainvoke 消费；
- FakeChatAgent：暴露 .graph.ainvoke，按脚本返回一轮消息（含 execute_sql 调用与最终回答），
  代替「Operation 步骤复用 ChatAgent 跑一轮」。
"""
import json
from typing import List, Optional

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.analysis_agent import AnalysisAgent
from app.tasks.events import TaskEvent


# ========== 假模型 ==========


class FakeScriptedLLM(BaseChatModel):
    """按脚本逐次返回 AIMessage 的假 LLM（Planner / Reflection 用）。"""

    scripted: List[AIMessage]
    step: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-scripted-llm"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        msg = self.scripted[min(self.step, len(self.scripted) - 1)]
        object.__setattr__(self, "step", self.step + 1)
        return ChatResult(generations=[ChatGeneration(message=msg)])


class _FakeGraph:
    """FakeChatAgent.graph：按脚本逐次返回一轮消息流。"""

    def __init__(self, io: List[dict]):
        self._io = io
        self._i = 0

    async def ainvoke(self, state, config=None, **kwargs):
        item = self._io[min(self._i, len(self._io) - 1)]
        self._i += 1
        return {"messages": _step_messages(item.get("answer", ""), item.get("sql"))}


class FakeChatAgent:
    """代替 ChatAgent：每个 Operation 步骤跑它的 graph.ainvoke 一轮。"""

    def __init__(self, io: List[dict]):
        self.graph = _FakeGraph(io)


def _step_messages(answer: str, sql: Optional[str]) -> List:
    """构造一轮 ChatAgent 消息流：可选 execute_sql 调用 + 最终回答。"""
    messages: List = []
    if sql:
        messages.append(
            AIMessage(content="", tool_calls=[{"name": "execute_sql", "args": {"sql": sql}, "id": "c1"}])
        )
        messages.append(ToolMessage(content="(查询结果)", tool_call_id="c1"))
    messages.append(AIMessage(content=answer))
    return messages


def _plan_msg(steps: List[dict]) -> AIMessage:
    return AIMessage(content=json.dumps(steps, ensure_ascii=False))


def _reflect_msg(assessment: str, conclusion: str, need_more=False, supplement=None) -> AIMessage:
    return AIMessage(
        content=json.dumps(
            {
                "assessment": assessment,
                "conclusion": conclusion,
                "need_more": need_more,
                "supplement": supplement or {},
            },
            ensure_ascii=False,
        )
    )


async def _collect_events(agent_kwargs) -> tuple:
    """跑一次 analyze，同时收集 on_event 事件序列。返回 (result, events)。"""
    events: List[TaskEvent] = []

    async def on_event(e: TaskEvent) -> None:
        events.append(e)

    agent = AnalysisAgent(on_event=on_event, **agent_kwargs)
    result = await agent.analyze("分析各州销售额分布")
    return result, events


# ========== 1. Planner：合法 JSON 全流程 + 报告结构 ==========


async def test_three_step_plan_runs_and_builds_report():
    llm = FakeScriptedLLM(
        scripted=[
            _plan_msg(
                [
                    {"goal": "统计各州订单总额", "tool_hint": "sqlite-query 对 orders 分组"},
                    {"goal": "找出销售额最高的州", "tool_hint": "排序取 top"},
                    {"goal": "对比前三名州的占比", "tool_hint": "计算占比"},
                ]
            ),
            _reflect_msg("数据已充分回答问题", "SP 州销售额最高，建议重点投放。", need_more=False),
        ]
    )
    chat = FakeChatAgent(
        io=[
            {"answer": "各州总额已算出", "sql": "SELECT customer_state, SUM(price) FROM orders GROUP BY 1"},
            {
                "answer": "SP 州最高",
                "sql": "SELECT customer_state FROM orders GROUP BY 1 ORDER BY SUM(price) DESC LIMIT 1",
            },
            {
                "answer": "前三名占比 80%",
                "sql": "SELECT customer_state, SUM(price) FROM orders GROUP BY 1 ORDER BY 2 DESC LIMIT 3",
            },
        ]
    )
    agent = AnalysisAgent(llm=llm, chat_agent=chat)
    result = await agent.analyze("分析各州销售额分布")

    # 三步都执行
    assert len(result["step_results"]) == 3
    assert [s["index"] for s in result["step_results"]] == [1, 2, 3]

    report = result["report"]
    # 结构：四大段标题齐全
    assert "# 数据分析报告" in report
    assert "## 概述" in report
    assert "## 各步发现" in report
    assert "## 结论与建议" in report
    assert "## 附：执行的 SQL 清单" in report
    # 各步标题
    assert "### 步骤 1：统计各州订单总额" in report
    assert "### 步骤 2：找出销售额最高的州" in report
    assert "### 步骤 3：对比前三名州的占比" in report
    # 结论正文来自 Reflection
    assert "SP 州销售额最高" in report
    # SQL 清单段收录了 3 条（每步 1 条）
    sql_section = report.split("## 附：执行的 SQL 清单")[1]
    assert "1. `SELECT customer_state, SUM(price) FROM orders GROUP BY 1`" in sql_section
    assert "2. `SELECT customer_state FROM orders GROUP BY 1 ORDER BY SUM(price) DESC LIMIT 1`" in sql_section
    assert "3. " in sql_section


# ========== 1'. Planner：非法 JSON 重试一次成功 ==========


async def test_planner_retries_once_then_succeeds():
    llm = FakeScriptedLLM(
        scripted=[
            AIMessage(content="抱歉我先想想，这不是 JSON"),  # 首次非法
            _plan_msg([{"goal": "查订单量", "tool_hint": "sqlite-query"}, {"goal": "查金额", "tool_hint": ""}]),
            _reflect_msg("已回答", "结论若干", need_more=False),
        ]
    )
    chat = FakeChatAgent(
        io=[{"answer": "订单量 3", "sql": "SELECT COUNT(*) FROM orders"}, {"answer": "金额 180", "sql": None}]
    )
    agent = AnalysisAgent(llm=llm, chat_agent=chat)
    result = await agent.analyze("分析订单")

    assert len(result["plan"]) == 2
    assert result["plan"][0]["goal"] == "查订单量"
    assert len(result["step_results"]) == 2
    # 第二步无 SQL：SQL 清单只应有 1 条
    assert result["report"].count("SELECT") == 1


# ========== 1''. Planner：重试仍失败 → 显式报错 ==========


async def test_planner_retry_exhausted_raises():
    llm = FakeScriptedLLM(
        scripted=[
            AIMessage(content="不是 JSON"),
            AIMessage(content="还是不是 JSON"),
        ]
    )
    chat = FakeChatAgent(io=[{"answer": "x", "sql": None}])
    agent = AnalysisAgent(llm=llm, chat_agent=chat)
    with pytest.raises(RuntimeError, match="无法生成合法 JSON 计划"):
        await agent.analyze("随便分析下")


# ========== 3. Reflection 触发一次补充步骤（且第二次不再补充） ==========


async def test_reflection_triggers_one_supplement_then_stops():
    llm = FakeScriptedLLM(
        scripted=[
            _plan_msg([{"goal": "统计总额", "tool_hint": "sqlite-query"}, {"goal": "找最高州", "tool_hint": ""}]),
            # 第一次反思：发现缺口，要求补充一步
            _reflect_msg(
                "缺少时间趋势",
                "初步结论",
                need_more=True,
                supplement={"goal": "按月份看销售额趋势", "tool_hint": "sqlite-query 按月分组"},
            ),
            # 第二次反思：即便仍说 need_more=True，也应被 supplement_used 拦下（防循环）
            _reflect_msg(
                "还想再补",
                "最终结论：整体向好",
                need_more=True,
                supplement={"goal": "再补一步", "tool_hint": "x"},
            ),
        ]
    )
    chat = FakeChatAgent(
        io=[
            {"answer": "总额已算", "sql": "SELECT SUM(price) FROM orders"},
            {"answer": "SP 最高", "sql": "SELECT customer_state FROM orders GROUP BY 1 ORDER BY 2 DESC"},
            {"answer": "各月趋势平稳", "sql": "SELECT strftime('%m', ts), SUM(price) FROM orders GROUP BY 1"},
        ]
    )
    agent = AnalysisAgent(llm=llm, chat_agent=chat)
    result = await agent.analyze("分析销售额")

    # 补充生效：2 步计划 + 1 步补充 = 3 步执行，绝不再多
    assert len(result["plan"]) == 3
    assert len(result["step_results"]) == 3
    assert result["plan"][2]["goal"] == "按月份看销售额趋势"
    # 报告含补充步骤标题与最终结论
    assert "### 步骤 3：按月份看销售额趋势" in result["report"]
    assert "整体向好" in result["report"]


# ========== 4. 进度事件序列 ==========


async def test_progress_event_phase_sequence():
    llm = FakeScriptedLLM(
        scripted=[
            _plan_msg([{"goal": "步骤A", "tool_hint": ""}, {"goal": "步骤B", "tool_hint": ""}]),
            _reflect_msg("已回答", "结论", need_more=False),
        ]
    )
    chat = FakeChatAgent(io=[{"answer": "A 完成", "sql": "SELECT 1"}, {"answer": "B 完成", "sql": None}])
    result, events = await _collect_events({"llm": llm, "chat_agent": chat})

    phases = [e.payload.get("phase") for e in events]
    assert phases == ["planning", "step", "step", "reflecting", "reporting", "done"]

    # 终结事件类型为 done，且携带各步摘要
    done = events[-1]
    assert done.type == "done"
    assert done.progress == 1.0
    assert [s["index"] for s in done.payload["steps"]] == [1, 2]
    # step 事件带 i/N 与命中的 SQL
    step1 = events[1]
    assert step1.payload["index"] == 1 and step1.payload["total"] == 2
    assert step1.payload["sql"] == ["SELECT 1"]
    # planning 事件带步骤目标清单
    assert events[0].payload["steps"] == ["步骤A", "步骤B"]


async def test_supplement_path_emits_extra_step_and_reflecting():
    """补充路径下事件序列：planning → step×2 → reflecting → step → reflecting → reporting → done。"""
    llm = FakeScriptedLLM(
        scripted=[
            _plan_msg([{"goal": "A", "tool_hint": ""}, {"goal": "B", "tool_hint": ""}]),
            _reflect_msg("缺一步", "初步", need_more=True, supplement={"goal": "C", "tool_hint": ""}),
            _reflect_msg("够了", "最终", need_more=False),
        ]
    )
    chat = FakeChatAgent(io=[{"answer": "a", "sql": None}, {"answer": "b", "sql": None}, {"answer": "c", "sql": None}])
    _, events = await _collect_events({"llm": llm, "chat_agent": chat})
    phases = [e.payload.get("phase") for e in events]
    assert phases == ["planning", "step", "step", "reflecting", "step", "reflecting", "reporting", "done"]


# ========== 5. routes_analysis：TestClient + 假 agent 注入 ==========


def test_route_analysis_with_fake_agent():
    from fastapi.testclient import TestClient

    from app.api.routes_analysis import get_analysis_agent
    from app.main import app

    class FakeAnalysisAgent:
        async def analyze(self, question, max_steps=None):
            assert max_steps == 2  # 同步小模式夹到 2
            return {
                "report": "# 数据分析报告\n\n## 概述\n- 原始问题：" + question,
                "plan": [{"goal": "查询", "tool_hint": "sqlite-query"}],
                "step_results": [],
                "reflection": "结论",
                "step_summaries": [{"index": 1, "goal": "查询", "answer_preview": "ok", "sql_count": 1}],
            }

    app.dependency_overrides[get_analysis_agent] = lambda: FakeAnalysisAgent()
    try:
        client = TestClient(app)
        resp = client.post("/api/analysis", json={"question": "各州销售额如何"})
        assert resp.status_code == 200
        body = resp.json()
        assert "# 数据分析报告" in body["report"]
        assert "各州销售额如何" in body["report"]
        assert body["steps"][0]["goal"] == "查询"
        assert body["plan"][0]["tool_hint"] == "sqlite-query"

        # max_steps 超过同步上限被 pydantic 拒（le=2）
        assert client.post("/api/analysis", json={"question": "x", "max_steps": 5}).status_code == 422
    finally:
        app.dependency_overrides.clear()


# ========== 6. worker：run_analysis_task 事件透传 + mark_done（离线 fakeredis） ==========


async def test_run_analysis_task_emits_phase_events(monkeypatch):
    import fakeredis.aioredis

    from app.core import dependencies as deps
    from app.tasks import worker
    from app.tasks.service import TaskService

    llm = FakeScriptedLLM(
        scripted=[
            _plan_msg([{"goal": "统计总额", "tool_hint": "sqlite-query"}, {"goal": "找最高州", "tool_hint": ""}]),
            _reflect_msg("已回答", "SP 最高，建议聚焦", need_more=False),
        ]
    )
    chat = FakeChatAgent(
        io=[{"answer": "总额已算", "sql": "SELECT SUM(price) FROM orders"}, {"answer": "SP 最高", "sql": None}]
    )

    async def _fake_get_chat_agent():
        return chat

    # 注入假 chat_agent 与假 LLM，绝不触真 LLM / 真 Redis
    monkeypatch.setattr(deps, "get_chat_agent", _fake_get_chat_agent)
    monkeypatch.setattr(worker.LLMFactory, "create_llm", lambda **kw: llm)

    text = fakeredis.aioredis.FakeRedis(decode_responses=True)
    svc = TaskService(redis=text)
    task_id = "analysis-1"
    ctx = {"job_id": task_id, "redis": text}

    out = await worker.run_analysis_task(ctx, question="分析各州销售额")
    assert "# 数据分析报告" in out["report"]

    events = [e["event"] for e in await svc.read_events(task_id)]
    types = [e["type"] for e in events]
    assert types[0] == "started"
    assert types[-1] == "done"
    # 阶段序列（started 是 worker 加的信封，phase 由 agent 上报）
    phases = [e["payload"].get("phase") for e in events if e["payload"].get("phase")]
    assert phases == ["planning", "step", "step", "reflecting", "reporting", "done"]
    # done 事件带各步摘要
    assert [s["index"] for s in events[-1]["payload"]["steps"]] == [1, 2]

    # 状态落终态，完整报告存进结果
    st = await svc.get_status(task_id)
    assert st["status"] == "done"
    assert "## 附：执行的 SQL 清单" in st["result"]["report"]


def test_route_analysis_planner_failure_returns_400():
    from fastapi.testclient import TestClient

    from app.api.routes_analysis import get_analysis_agent
    from app.main import app

    class BoomAgent:
        async def analyze(self, question, max_steps=None):
            raise RuntimeError("Planner 无法生成合法 JSON 计划（已重试一次）：boom")

    app.dependency_overrides[get_analysis_agent] = lambda: BoomAgent()
    try:
        client = TestClient(app)
        resp = client.post("/api/analysis", json={"question": "坏问题"})
        assert resp.status_code == 400
        assert "无法生成合法 JSON 计划" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()
