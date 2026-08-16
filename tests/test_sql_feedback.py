"""SQL 执行结果记录器（feedback.py）与对话回流下发测试。

覆盖：
- recorder 生命周期：无 recorder 时工具直调不炸（record 为空操作）
- execute_sql 成功路径写入 record（演示库路径）；失败路径不写
- 多轮自纠：取最后一次成功（用户最终看到的结果）
- ChatService 流式/非流式下发 sql_result；未执行 SQL 时不下发（旧事件序列不变）
"""

from __future__ import annotations

import json

import pytest

from app.agents.tools.sql_tool import create_execute_sql_tool
from app.services.chat_service import ChatService
from app.text2sql.feedback import latest_successful_sql, use_sql_recorder

# ========== recorder 与工具直录 ==========


def test_record_is_noop_without_recorder(demo_db):
    """单测/评测直调工具（无 recorder）不受影响。"""
    execute_sql = create_execute_sql_tool(demo_db)
    out = execute_sql.invoke({"sql": "SELECT SUM(price) FROM orders"})
    assert json.loads(out)["row_count"] == 1
    assert latest_successful_sql() is None


def test_successful_execution_recorded(demo_db):
    """成功路径写入 record；校验失败路径不写。"""
    execute_sql = create_execute_sql_tool(demo_db)
    with use_sql_recorder("订单总价是多少"):
        bad = execute_sql.invoke({"sql": "SELECT nonexistent_col FROM orders"})
        assert bad.startswith("SQL 校验失败")
        assert latest_successful_sql() is None

        ok = execute_sql.invoke({"sql": "SELECT SUM(price) FROM orders"})
        assert json.loads(ok)["row_count"] == 1
        record = latest_successful_sql()

    assert record is not None
    assert record.question == "订单总价是多少"
    assert "SUM(price)" in record.sql
    assert record.row_count == 1
    assert record.datasource_id is None  # 演示库路径
    # 退出 recorder 后读取为空（上下文已复位）
    assert latest_successful_sql() is None


def test_latest_success_wins_across_retries(demo_db):
    """多轮自纠：第一次成功后模型又改写再执行，记录取最后一次成功。"""
    execute_sql = create_execute_sql_tool(demo_db)
    with use_sql_recorder("各州订单数"):
        execute_sql.invoke({"sql": "SELECT customer_state FROM orders LIMIT 5"})
        execute_sql.invoke({"sql": "SELECT customer_state, COUNT(*) AS cnt FROM orders GROUP BY customer_state"})

    record = latest_successful_sql()
    assert record is None  # 上下文外读取为空

    with use_sql_recorder("各州订单数"):
        execute_sql.invoke({"sql": "SELECT customer_state, COUNT(*) AS cnt FROM orders GROUP BY customer_state"})
        assert "GROUP BY" in latest_successful_sql().sql


# ========== ChatService 下发 ==========


class _SessionStore:
    def __init__(self):
        self.messages = []

    def get_history(self, _session_id):
        return []

    def get_summary(self, _session_id):
        return ""

    def add_message(self, session_id, role, content):
        self.messages.append((session_id, role, content))


class _Agent:
    """fake agent：chat_stream 内直调工具，模拟 Agent 循环里执行 SQL。"""

    def __init__(self, tool):
        self._tool = tool

    async def chat(self, question, history=None, summary=""):
        self._tool.invoke({"sql": "SELECT SUM(price) FROM orders"})
        return "答案"

    async def chat_stream(self, question, history=None, summary=""):
        yield {"type": "content", "text": "答案"}
        self._tool.invoke({"sql": "SELECT SUM(price) FROM orders"})


class _NoSqlAgent:
    async def chat(self, question, history=None, summary=""):
        return "答案"

    async def chat_stream(self, question, history=None, summary=""):
        yield {"type": "content", "text": "答案"}


@pytest.mark.asyncio
async def test_chat_stream_emits_sql_result(demo_db):
    """流式：sources 后追加 sql_result 事件（旧前端忽略未知类型）。"""
    execute_sql = create_execute_sql_tool(demo_db)
    service = ChatService(_Agent(execute_sql), _SessionStore())

    events = [event async for event in service.chat_stream("s1", "订单总价")]

    assert events[-2] == {"type": "sources", "data": []}
    sql_event = events[-1]
    assert sql_event["type"] == "sql_result"
    assert "SUM(price)" in sql_event["data"]["sql"]
    assert sql_event["data"]["question"] == "订单总价"
    assert sql_event["data"]["datasource_id"] is None


@pytest.mark.asyncio
async def test_chat_stream_without_sql_keeps_legacy_events(demo_db):
    """未执行 SQL：事件序列与改造前完全一致（无 sql_result）。"""
    service = ChatService(_NoSqlAgent(), _SessionStore())
    events = [event async for event in service.chat_stream("s1", "问题")]
    assert events == [
        {"type": "content", "data": "答案"},
        {"type": "sources", "data": []},
    ]


@pytest.mark.asyncio
async def test_chat_returns_sql_result(demo_db):
    """非流式：响应 dict 带可选 sql_result。"""
    execute_sql = create_execute_sql_tool(demo_db)
    service = ChatService(_Agent(execute_sql), _SessionStore())

    result = await service.chat("s1", "订单总价")
    assert result["sql_result"]["sql"].startswith("SELECT SUM(price)")
    assert result["sql_result"]["row_count"] == 1

    service2 = ChatService(_NoSqlAgent(), _SessionStore())
    result2 = await service2.chat("s1", "问题")
    assert result2["sql_result"] is None
