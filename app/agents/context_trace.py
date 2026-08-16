"""请求级上下文轨迹记录器（聊天可解释性 UI 的信号侧）。

两个 ContextVar：
- ``_trace``          请求级 ContextTrace（ChatService 进入 use_context_trace）；
- ``_active_tool_trace`` 当前执行中的 ToolCallTrace——middleware 在 handler 外包
  ``use_active_tool_trace``，工具内的 record_*_hits 自行读取，不显式接收 call_id
  （sync 工具执行线程 / async 并发 / MCP override 都经 langchain 的 copy_context 归位）。

与 app/text2sql/feedback.py 是同一种模式的两种语义：那边取**最后一次成功**（SQL 沉淀
要用户最终看到的结果），这边**追加保留全过程**（可解释性要过程——"检索两次、第一次
没命中"本身就是信息）。

安全边界：工具参数与异常不能原文下发浏览器。参数先脱敏（键名 + 字符串级 DSN/Bearer/
Authorization 模式）再截断；错误只传稳定枚举 error_code + 本模块独立安全映射的中文
public_message，原始 str(exc) 只留后端日志/Langfuse。
"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, Optional, Sequence

# 展示层细节，刻意不进 Settings（feedback.py 同款零 Settings 依赖）
ARGS_MAX_CHARS = 160  # 工具参数摘要截断（脱敏之后）
SNIPPET_MAX_CHARS = 120  # 文档片段摘要截断
MAX_TOOL_CALLS = 30  # 请求级上限：单请求最多记录的调用数
MAX_HITS_PER_TYPE = 20  # 请求级上限：每类命中按整个请求累计（非每调用各一份）

# 键名脱敏：命中即整值替换（含嵌套 dict/list 递归）
_SENSITIVE_KEY_MARKS = (
    "password",
    "token",
    "api_key",
    "secret",
    "authorization",
    "cookie",
    "headers",
    "credential",
)

# 字符串级脱敏：值本身含敏感形态（DSN 密码段 / Bearer / Authorization 头）
_DSN_PASSWORD_RE = re.compile(r"(://[^:/@\s]+:)[^@/\s]+(@)")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+\S+")


@dataclass(frozen=True)
class TermHit:
    hit_key: str
    rank: int
    term: str
    definition: str


@dataclass(frozen=True)
class ExampleHit:
    hit_key: str
    rank: int
    question: str
    sql: str


@dataclass(frozen=True)
class DocHit:
    hit_key: str
    rank: int
    source: str
    title: str
    snippet: str


@dataclass(frozen=True)
class GraphHit:
    hit_key: str
    rank: int
    kind: str  # entity / path
    query: str
    summary: str
    result_count: int


@dataclass
class Hits:
    """单次工具调用的命中明细（嵌套在 ToolCallTrace 下，非全局列表）。"""

    terms: list[TermHit] = field(default_factory=list)
    examples: list[ExampleHit] = field(default_factory=list)
    docs: list[DocHit] = field(default_factory=list)
    graph: list[GraphHit] = field(default_factory=list)


@dataclass
class ToolCallTrace:
    """一次工具调用的轨迹（可变：finish 原地补全；started_at 只作内部计时）。"""

    call_id: str
    name: str
    args: str
    started_at: float = field(default_factory=time.monotonic)
    duration_ms: Optional[int] = None
    status: str = "running"
    attempts: Optional[int] = None
    error_code: Optional[str] = None
    public_message: Optional[str] = None
    hits: Hits = field(default_factory=Hits)


@dataclass
class ContextTrace:
    """一次对话请求的完整轨迹。上限按整个请求累计，触发置 truncated。"""

    tool_calls: list[ToolCallTrace] = field(default_factory=list)
    truncated: bool = False
    # 请求级累计计数（含被上限丢弃的，保证计数口径）
    _term_count: int = 0
    _example_count: int = 0
    _doc_count: int = 0
    _graph_count: int = 0


_trace: ContextVar[Optional[ContextTrace]] = ContextVar("context_trace", default=None)
_active_tool_trace: ContextVar[Optional[ToolCallTrace]] = ContextVar("active_tool_trace", default=None)


@contextmanager
def use_context_trace() -> Iterator[None]:
    """进入一次对话请求的轨迹上下文（ChatService 的 chat/chat_stream 调用）。"""
    token = _trace.set(ContextTrace())
    try:
        yield
    finally:
        _trace.reset(token)


@contextmanager
def use_active_tool_trace(trace: ToolCallTrace) -> Iterator[None]:
    """标记"当前正在执行的工具调用"（ToolRuntimeMiddleware 包住 handler 时进入）。

    工具内的 record_*_hits 经它归位到正确的调用，是 call_id 的传递通道。
    """
    token = _active_tool_trace.set(trace)
    try:
        yield
    finally:
        _active_tool_trace.reset(token)


# ========== 脱敏 ==========


def _redact_value(value, key_hint: str = "") -> object:
    """递归脱敏：敏感键的值整体替换；字符串做 DSN/Bearer 模式脱敏；dict/list 递归。"""
    if isinstance(value, dict):
        return {k: _redact_value(v, key_hint=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v, key_hint=key_hint) for v in value]
    if not isinstance(value, str):
        return "***" if any(mark in key_hint.lower() for mark in _SENSITIVE_KEY_MARKS) else value
    if any(mark in key_hint.lower() for mark in _SENSITIVE_KEY_MARKS):
        return "***"
    # 字符串级：DSN 密码段 / Bearer token（Authorization: xxx 由键名脱敏兜底）
    return _BEARER_RE.sub(r"\1 ***", _DSN_PASSWORD_RE.sub(r"\1***\2", value))


def summarize_args(args: object) -> str:
    """工具参数 → 脱敏后 JSON 摘要，再截断到 ARGS_MAX_CHARS（顺序：先脱敏后截断）。"""
    try:
        redacted = _redact_value(args)
        text = json.dumps(redacted, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(args)
    if len(text) > ARGS_MAX_CHARS:
        return text[:ARGS_MAX_CHARS] + "…"
    return text


# status → 稳定错误枚举与安全公开文案（独立映射，不复用 ToolRuntime 的降级文案——
# 那边拼接原始异常原文，不能下发浏览器）
_ERROR_CODE_BY_STATUS = {
    "timeout": ("timeout", "工具执行超时，已跳过"),
    "error": ("tool_failure", "工具执行失败，已降级处理"),
    "circuit_open": ("circuit_open", "工具熔断保护中，暂不可用"),
}
_UNKNOWN_ERROR = ("unknown", "工具执行异常，已降级处理")


def _error_of(status: str) -> tuple[str, str]:
    return _ERROR_CODE_BY_STATUS.get(status, _UNKNOWN_ERROR)


# ========== 记录 API（无 recorder / 无 active trace 一律空操作） ==========


def record_tool_start(call_id: str, name: str, args: object) -> Optional[ToolCallTrace]:
    """middleware 在工具执行前调用；返回句柄（无请求级 recorder 时 None）。"""
    trace = _trace.get()
    if trace is None:
        return None
    if len(trace.tool_calls) >= MAX_TOOL_CALLS:
        trace.truncated = True
        return None
    call = ToolCallTrace(call_id=call_id, name=name, args=summarize_args(args))
    trace.tool_calls.append(call)
    return call


def finish_tool_call(
    call: Optional[ToolCallTrace],
    *,
    status: str,
    attempts: Optional[int] = None,
) -> None:
    """middleware 在工具执行后调用，原地补全（句柄为 None 时空操作）。"""
    if call is None:
        return
    call.duration_ms = int((time.monotonic() - call.started_at) * 1000)
    call.status = status
    call.attempts = attempts
    if status != "success":
        call.error_code, call.public_message = _error_of(status)


def _active_call() -> Optional[ToolCallTrace]:
    return _active_tool_trace.get()


def _over_limit(kind: str) -> bool:
    """请求级每类累计上限；超限丢弃该条并置 truncated。"""
    trace = _trace.get()
    if trace is None:
        return True
    count = getattr(trace, f"_{kind}_count")
    if count >= MAX_HITS_PER_TYPE:
        trace.truncated = True
        return True
    setattr(trace, f"_{kind}_count", count + 1)
    return False


def record_term_hits(hits: Sequence[dict]) -> None:
    """sql_context_search 命中的术语明细（term/definition）。"""
    call = _active_call()
    if call is None:
        return
    for rank, t in enumerate(hits, 1):
        if _over_limit("term"):
            return
        call.hits.terms.append(
            TermHit(hit_key=t.get("term", ""), rank=rank, term=t.get("term", ""), definition=t.get("definition", ""))
        )


def record_example_hits(hits: Sequence[dict]) -> None:
    """sql_context_search 命中的示例明细（question/sql——SQL 本就是注入模型的 few-shot，全记）。"""
    call = _active_call()
    if call is None:
        return
    for rank, ex in enumerate(hits, 1):
        if _over_limit("example"):
            return
        call.hits.examples.append(
            ExampleHit(hit_key=ex.get("question", ""), rank=rank, question=ex.get("question", ""), sql=ex.get("sql", ""))
        )


def record_doc_hits(docs: Sequence[dict]) -> None:
    """知识库检索命中文档明细（source/title/片段摘要；无 score——RRF 融合后口径不可比）。

    hit_key 复用 app/rag/document_utils.document_key 的语义：优先 source+chunk_index，
    缺失时退化为 source 序号——保证同一文档重复检索可被摘要去重。
    """
    call = _active_call()
    if call is None:
        return
    for rank, doc in enumerate(docs, 1):
        if _over_limit("doc"):
            return
        source = str(doc.get("source") or "")
        chunk_index = doc.get("chunk_index")
        hit_key = f"{source}:{chunk_index}" if chunk_index is not None else f"{source}:{rank}"
        content = str(doc.get("content") or doc.get("text") or "")
        call.hits.docs.append(
            DocHit(
                hit_key=hit_key,
                rank=rank,
                source=source,
                title=str(doc.get("title") or source),
                snippet=content[:SNIPPET_MAX_CHARS],
            )
        )


def record_graph_hit(kind: str, query: str, summary: str, result_count: int) -> None:
    """graph_search / graph_path_search 的查询摘要与结果规模（不记全量边）。"""
    call = _active_call()
    if call is None:
        return
    if _over_limit("graph"):
        return
    call.hits.graph.append(
        GraphHit(hit_key=f"{kind}:{query}", rank=len(call.hits.graph) + 1, kind=kind, query=query, summary=summary, result_count=result_count)
    )


# ========== 读取 ==========


def current_context_trace() -> Optional[ContextTrace]:
    return _trace.get()


def context_hits_payload() -> Optional[dict]:
    """轨迹 → SSE/响应负载；无 recorder 或轨迹为空返回 None（不下发该事件）。

    摘要计数按 hit_key 去重（详情保留每次调用过程，摘要不重复计）。
    """
    trace = _trace.get()
    if trace is None or not trace.tool_calls:
        return None

    tool_calls = [
        {
            "call_id": c.call_id,
            "name": c.name,
            "args": c.args,
            "duration_ms": c.duration_ms,
            "status": c.status,
            "attempts": c.attempts,
            "error_code": c.error_code,
            "public_message": c.public_message,
            "hits": {
                "terms": [t.__dict__ for t in c.hits.terms],
                "examples": [e.__dict__ for e in c.hits.examples],
                "docs": [d.__dict__ for d in c.hits.docs],
                "graph": [g.__dict__ for g in c.hits.graph],
            },
        }
        for c in trace.tool_calls
    ]
    # 摘要：按 hit_key 去重计数（跨调用去重——"命中几个不同示例"）
    deduped = {
        "terms": {h.hit_key for c in trace.tool_calls for h in c.hits.terms},
        "examples": {h.hit_key for c in trace.tool_calls for h in c.hits.examples},
        "docs": {h.hit_key for c in trace.tool_calls for h in c.hits.docs},
        "graph": {h.hit_key for c in trace.tool_calls for h in c.hits.graph},
    }
    deduped = {kind: len(keys) for kind, keys in deduped.items()}
    return {"tool_calls": tool_calls, "summary": deduped, "truncated": trace.truncated}
