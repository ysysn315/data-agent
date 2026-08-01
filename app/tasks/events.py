"""异步任务的进度事件模型。

TaskEvent 是任务进度的统一结构：worker 用 XADD 追加到 Redis Stream，SSE 端点按序
读出后原样透传给前端。字段刻意做小、与具体任务解耦 —— 无论是对话任务还是评估任务，
进度都压进这四个字段里，前端只认一种事件 schema。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskEvent(BaseModel):
    """一条任务进度事件。

    - type: 事件类型。约定 started（开始）/ progress（进度）/ done（完成）/ error（失败）。
      done、error 为终结事件，SSE 读到即结束订阅。
    - message: 给人看的一句话进度，如「第 3/10 例 ✓」「已激活技能: sql-generation」。
    - progress: 0~1 的完成比例，未知填 None。
    - payload: 结构化附加数据（如 {"accuracy": 0.8}），供前端渲染，不塞大对象。
    """

    type: str
    message: str = ""
    progress: Optional[float] = None
    payload: dict[str, Any] = Field(default_factory=dict)


# 终结事件类型：SSE 读到其一即可结束订阅并关闭连接
TERMINAL_EVENT_TYPES = {"done", "error"}
