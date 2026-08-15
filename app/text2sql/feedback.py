"""SQL 执行结果记录器（对话回流闭环的信号侧）。

工具内直录 ContextVar（先例：current_selection 路由写工具读、current_sources
工具写服务读），而不是事后解析 ToolMessage——结果 JSON 里没有 SQL，SQL 在
AIMessage.tool_calls 里，流式下 tool_call_chunks 分片到达聚合不可靠。

用法（ChatService）：
    with use_sql_recorder(question=question):
        ... Agent 执行（execute_sql 成功路径会 record）...
    record = latest_successful_sql()  # 取最后一次成功（即用户最终看到的结果）

无 recorder 时 record_sql_execution() 是空操作——单测直接调工具不受影响。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, Optional


@dataclass(frozen=True)
class SQLExecutionRecord:
    """一次成功执行的 SQL 及其摘要（供前端「沉淀为示例」与库内 meta 使用）。"""

    question: str
    sql: str
    row_count: int
    columns: list[str] = field(default_factory=list)
    datasource_id: Optional[int] = None


@dataclass
class _SQLRecorder:
    """一次对话请求内的记录器：只保留最后一次成功执行。"""

    question: str
    latest: Optional[SQLExecutionRecord] = None


_recorder: ContextVar[Optional[_SQLRecorder]] = ContextVar("sql_recorder", default=None)


@contextmanager
def use_sql_recorder(question: str) -> Iterator[None]:
    """进入一次对话请求的 SQL 记录上下文（ChatService 的 chat/chat_stream 调用）。"""
    token = _recorder.set(_SQLRecorder(question=question))
    try:
        yield
    finally:
        _recorder.reset(token)


def record_sql_execution(sql: str, row_count: int, columns: list[str], datasource_id: Optional[int]) -> None:
    """execute_sql 成功路径调用；无 recorder 时（单测/评测直调工具）为空操作。"""
    recorder = _recorder.get()
    if recorder is None:
        return
    recorder.latest = SQLExecutionRecord(
        question=recorder.question,
        sql=sql,
        row_count=row_count,
        columns=list(columns),
        datasource_id=datasource_id,
    )


def latest_successful_sql() -> Optional[SQLExecutionRecord]:
    """取当前上下文最后一次成功执行（多轮自纠时用户最终看到的那条）。"""
    recorder = _recorder.get()
    return recorder.latest if recorder is not None else None
