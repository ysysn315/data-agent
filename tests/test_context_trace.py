"""context_trace 记录器测试（基础节：生命周期/脱敏/上限/负载）。

记录点接入、middleware 轨迹、SSE 下发的测试在后续 commit 随接线补齐；
本文件先锁死记录器自身的语义契约。
"""

from __future__ import annotations

import pytest

from app.agents.context_trace import (
    ARGS_MAX_CHARS,
    MAX_HITS_PER_TYPE,
    MAX_TOOL_CALLS,
    context_hits_payload,
    current_context_trace,
    finish_tool_call,
    record_doc_hits,
    record_example_hits,
    record_term_hits,
    record_tool_start,
    summarize_args,
    use_active_tool_trace,
    use_context_trace,
)

# ========== 生命周期 ==========


def test_record_is_noop_without_recorder():
    """无请求级 recorder 时所有记录 API 空操作（单测直调工具不受影响）。"""
    assert record_tool_start("c1", "execute_sql", {"sql": "SELECT 1"}) is None
    finish_tool_call(None, status="success")  # 不炸
    record_term_hits([{"term": "GMV", "definition": "d"}])  # 无 active trace，空操作
    assert context_hits_payload() is None


def test_trace_resets_after_context_exit():
    """退出 use_context_trace 后读取为空。"""
    with use_context_trace():
        assert current_context_trace() is not None
    assert current_context_trace() is None
    assert context_hits_payload() is None


# ========== 脱敏 ==========


def test_summarize_args_redacts_sensitive_keys_recursively():
    """键名脱敏：嵌套 dict/list 的敏感键值全替换，普通值保留。"""
    args = {
        "sql": "SELECT 1",
        "password": "hunter2",
        "config": {"api_key": "sk-123", "nested": [{"token": "abc"}, {"name": "ok"}]},
    }
    text = summarize_args("execute_sql", args)
    assert "hunter2" not in text and "sk-123" not in text and "abc" not in text
    assert "***" in text
    assert "SELECT 1" in text and "ok" in text


def test_summarize_args_redacts_dsn_and_bearer_strings():
    """字符串级脱敏：DSN 密码段 / Bearer token / Authorization 值。"""
    text = summarize_args("execute_sql", {"dsn": "postgres://user:s3cret@db.host:5432/prod"})
    assert "s3cret" not in text and "db.host" in text

    text = summarize_args("execute_sql", {"auth": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"})
    assert "eyJhbGciOiJIUzI1NiJ9" not in text and "Bearer ***" in text

    text = summarize_args("execute_sql", {"headers": {"Authorization": "Bearer abc.def"}})
    assert "abc.def" not in text


def test_summarize_args_truncates_after_redaction():
    """截断在脱敏之后：长文本截到 ARGS_MAX_CHARS，且敏感内容不在前缀里。"""
    args = {"log": "x" * 500, "secret": "leak-me"}
    text = summarize_args("execute_sql", args)
    assert len(text) == ARGS_MAX_CHARS + 1  # 截断 + 省略号
    assert text.endswith("…")
    assert "leak-me" not in text  # 脱敏先于截断


# ========== 错误安全映射 ==========


def test_finish_tool_call_maps_error_safely():
    """失败状态 → 稳定 error_code + 独立中文 public_message；不涉及原始异常。

    键对齐 ToolRuntime 实际终态：degraded（普通失败）/ circuit_open / cancelled。
    """
    with use_context_trace():
        call = record_tool_start("c1", "execute_sql", {"sql": "SELECT 1"})
        finish_tool_call(call, status="degraded", attempts=2)
        assert call.error_code == "tool_failure" and "失败" in call.public_message

        call2 = record_tool_start("c2", "t", {})
        finish_tool_call(call2, status="circuit_open")
        assert call2.error_code == "circuit_open"

        call3 = record_tool_start("c3", "t", {})
        finish_tool_call(call3, status="cancelled")
        assert call3.error_code == "cancelled"

        call4 = record_tool_start("c4", "t", {})
        finish_tool_call(call4, status="weird_unknown")  # 未识别状态
        assert call4.error_code == "unknown"

        call5 = record_tool_start("c5", "t", {})
        finish_tool_call(call5, status="success")
        assert call5.error_code is None and call5.public_message is None


# ========== 调用级命中与去重 ==========


def test_hits_attach_to_active_call_and_dedup_summary():
    """命中经 _active_tool_trace 归位到调用；摘要按 hit_key 去重计数。"""
    with use_context_trace():
        c1 = record_tool_start("c1", "sql_context_search", {"question": "各州 GMV"})
        with use_active_tool_trace(c1):
            record_term_hits([{"term": "GMV", "definition": "成交总额"}])
            record_example_hits([{"question": "各州客户数", "sql": "SELECT 1"}])
        c2 = record_tool_start("c2", "sql_context_search", {"question": "GMV 怎么算"})
        with use_active_tool_trace(c2):
            # 同一示例第二次检索：详情各留一条，摘要去重为 1
            record_example_hits([{"question": "各州客户数", "sql": "SELECT 1"}])

        payload = context_hits_payload()
    assert payload is not None
    assert len(payload["tool_calls"]) == 2
    all_examples = [e for c in payload["tool_calls"] for e in c["hits"]["examples"]]
    assert len(all_examples) == 2  # 详情保留过程
    assert all_examples[0]["hit_key"] == "各州客户数"
    assert payload["summary"]["examples"] == 1  # 摘要去重
    assert payload["summary"]["terms"] == 1


# ========== 请求级上限 ==========


def test_request_level_limits_set_truncated():
    """上限按整个请求累计：超限丢弃并置 truncated。"""
    with use_context_trace():
        # 工具调用数超限
        for i in range(MAX_TOOL_CALLS + 2):
            record_tool_start(f"c{i}", "t", {})
        payload = context_hits_payload()
    assert payload is not None
    assert len(payload["tool_calls"]) == MAX_TOOL_CALLS
    assert payload["truncated"] is True

    with use_context_trace():
        call = record_tool_start("c1", "t", {})
        with use_active_tool_trace(call):
            # 命中按请求累计超限（跨调用累计，非每调用各 20）
            for i in range(MAX_HITS_PER_TYPE + 5):
                record_term_hits([{"term": f"t{i}", "definition": "d"}])
        assert len(call.hits.terms) == MAX_HITS_PER_TYPE
        assert current_context_trace().truncated is True


def test_empty_payload_is_none():
    """recorder 内但零调用 → payload None（不下发事件）。"""
    with use_context_trace():
        assert context_hits_payload() is None


# ========== 记录点接入与真实执行路径 ==========


def test_sql_context_tool_records_hits(tmp_path):
    """sql_context_search 命中明细经 active trace 归位（无 recorder 时空操作不炸）。"""
    from app.agents.tools.sql_context_tool import create_sql_context_tool
    from app.text2sql.examples import ExampleStore
    from app.text2sql.terminology import TermStore

    tool = create_sql_context_tool(ExampleStore(tmp_path / "e.json"), TermStore(tmp_path / "t.json"))

    # 无 recorder：直调不炸
    tool.invoke({"question": "各州的复购率是多少"})

    with use_context_trace():
        call = record_tool_start("c1", "sql_context_search", {"question": "各州的复购率是多少"})
        with use_active_tool_trace(call):
            tool.invoke({"question": "各州的复购率是多少"})
        payload = context_hits_payload()
    hits = payload["tool_calls"][0]["hits"]
    assert any(t["term"] == "复购率" for t in hits["terms"])
    assert hits["examples"]  # 演示库示例命中


async def test_doc_tool_records_hits_and_keeps_sources(tmp_path):
    """知识库工具：doc 明细记录 + 粗粒度 sources 事件行为不变。"""
    from app.agents.tools.internal_docs_tool import create_docs_tool
    from app.rag.context import current_sources

    class _Retriever:
        async def retrieve_multi_query(self, query, top_k=3, metadata_filters=None):
            return [
                {"content": "运维手册片段A" * 10, "metadata": {"source": "ops.md", "title": "运维", "chunk_index": 1}},
                {"content": "运维手册片段B", "metadata": {"source": "ops.md", "title": "运维", "chunk_index": 2}},
            ]

    docs_tool = create_docs_tool(_Retriever())
    with use_context_trace():
        call = record_tool_start("c1", "query_internal_docs", {"query": "部署步骤"})
        with use_active_tool_trace(call):
            out = await docs_tool.ainvoke({"query": "部署步骤"})
        payload = context_hits_payload()

    assert "运维手册片段A" in out
    hits = payload["tool_calls"][0]["hits"]["docs"]
    assert len(hits) == 2
    assert hits[0]["source"] == "ops.md" and hits[0]["hit_key"] == "ops.md:1"
    assert "score" not in hits[0]  # score 刻意不记（口径不可比）
    # 粗粒度 sources 不受影响
    assert current_sources() == []  # 已退出 rag 上下文
    assert payload["summary"]["docs"] == 2


async def test_sync_tool_ainvoke_propagates_contextvar():
    """真实 LangChain 路径：sync Tool 经 ainvoke（run_in_executor + copy_context）读得到 recorder。"""
    from langchain_core.tools import tool as lc_tool

    @lc_tool
    def probe(x: str) -> str:
        """测试探针：记录一个术语命中。"""
        record_term_hits([{"term": x, "definition": "d"}])
        return "ok"

    with use_context_trace():
        call = record_tool_start("c1", "probe", {"x": "GMV"})
        with use_active_tool_trace(call):
            out = await probe.ainvoke({"x": "GMV"})
        payload = context_hits_payload()
    assert out == "ok"
    hits = payload["tool_calls"][0]["hits"]["terms"]
    assert hits and hits[0]["term"] == "GMV"  # 执行线程里读到了 active trace


async def test_concurrent_same_name_tools_do_not_cross():
    """两个同名工具并发调用：hits 按 active trace 归位不串。"""
    import asyncio

    from langchain_core.tools import tool as lc_tool

    @lc_tool
    def probe(x: str) -> str:
        """测试探针：记录一个术语命中。"""
        record_term_hits([{"term": x, "definition": "d"}])
        return x

    async def run_one(call_id: str, value: str) -> None:
        call = record_tool_start(call_id, "probe", {"x": value})
        with use_active_tool_trace(call):
            await probe.ainvoke({"x": value})

    with use_context_trace():
        await asyncio.gather(run_one("c1", "GMV"), run_one("c2", "复购率"))
        payload = context_hits_payload()

    by_id = {c["call_id"]: c for c in payload["tool_calls"]}
    assert [t["term"] for t in by_id["c1"]["hits"]["terms"]] == ["GMV"]
    assert [t["term"] for t in by_id["c2"]["hits"]["terms"]] == ["复购率"]


async def test_middleware_records_success_and_degradation():
    """middleware 轨迹：成功记 status/duration；失败记 error_code/public_message，原文不泄漏。"""
    from app.agents.middlewares import ToolRuntimeMiddleware
    from app.agents.tool_runtime import reset_tool_runtime_state

    class _Request:
        def __init__(self, name, args, call_id):
            self.tool_call = {"name": name, "args": args, "id": call_id}

    async def ok_handler(request):
        return type("M", (), {"content": "结果"})()

    async def bad_handler(request):
        raise RuntimeError("连接失败: postgres://admin:p@ss@10.0.0.1:5432/prod")

    mw = ToolRuntimeMiddleware()
    reset_tool_runtime_state()
    with use_context_trace():
        await mw.awrap_tool_call(_Request("execute_sql", {"sql": "SELECT 1"}, "c1"), ok_handler)
        await mw.awrap_tool_call(_Request("execute_sql", {"sql": "SELECT 1"}, "c2"), bad_handler)
        payload = context_hits_payload()

    by_id = {c["call_id"]: c for c in payload["tool_calls"]}
    ok = by_id["c1"]
    assert ok["status"] == "success" and ok["duration_ms"] is not None and ok["error_code"] is None

    bad = by_id["c2"]
    assert bad["status"] != "success"
    assert bad["error_code"] in ("tool_failure", "circuit_open", "cancelled", "unknown")
    assert bad["public_message"] and "p@ss" not in bad["public_message"]
    assert "10.0.0.1" not in bad["public_message"]  # 原始异常只在后端日志
    # args 摘要经脱敏（本例 SQL 无敏感键，原样保留）
    assert "SELECT 1" in ok["args"]


# ========== SSE / 响应下发 ==========


class _TraceSessionStore:
    def get_history(self, _sid):
        return []

    def get_summary(self, _sid):
        return ""

    def add_message(self, *_a):
        pass


class _TraceAgent:
    """fake agent：chat_stream 内模拟 middleware 记录 + 工具命中。"""

    async def chat(self, question, history=None, summary=""):
        call = record_tool_start("call_1", "sql_context_search", {"question": question})
        with use_active_tool_trace(call):
            record_term_hits([{"term": "GMV", "definition": "成交总额"}])
        finish_tool_call(call, status="success", attempts=1)
        return "答案"

    async def chat_stream(self, question, history=None, summary=""):
        yield {"type": "content", "text": "答案"}
        call = record_tool_start("call_1", "sql_context_search", {"question": question})
        with use_active_tool_trace(call):
            record_term_hits([{"term": "GMV", "definition": "成交总额"}])
        finish_tool_call(call, status="success", attempts=1)


async def test_chat_service_emits_context_hits_after_sql_result():
    """流末顺序 sources → sql_result（条件）→ context_hits（条件）。"""
    from app.services.chat_service import ChatService

    service = ChatService(_TraceAgent(), _TraceSessionStore())
    events = [event async for event in service.chat_stream("s1", "GMV 怎么算")]

    assert [e["type"] for e in events] == ["content", "sources", "context_hits"]
    payload = events[-1]["data"]
    assert payload["summary"]["terms"] == 1
    assert payload["tool_calls"][0]["name"] == "sql_context_search"


async def test_chat_service_without_tools_keeps_legacy_events():
    """无工具调用：事件序列与改造前完全一致（无 context_hits）。"""
    from app.services.chat_service import ChatService

    class _PlainAgent:
        async def chat(self, q, history=None, summary=""):
            return "答案"

        async def chat_stream(self, q, history=None, summary=""):
            yield {"type": "content", "text": "答案"}

    service = ChatService(_PlainAgent(), _TraceSessionStore())
    events = [event async for event in service.chat_stream("s1", "问题")]
    assert events == [
        {"type": "content", "data": "答案"},
        {"type": "sources", "data": []},
    ]
    result = await service.chat("s1", "问题")
    assert result["context_hits"] is None


def test_context_hits_serialization_contract():
    """序列化契约：SSE json.dumps 与 Pydantic ChatResponse 双向字段齐全、无敏感字段。"""
    import json

    from app.schemas.chat import ChatResponse

    with use_context_trace():
        call = record_tool_start("c1", "t", {"password": "x", "q": "问题"})
        with use_active_tool_trace(call):
            record_term_hits([{"term": "GMV", "definition": "d"}])
            record_doc_hits([{"source": "ops.md", "title": "运维", "chunk_index": 1, "content": "片段"}])
        finish_tool_call(call, status="degraded", attempts=2)
        payload = context_hits_payload()

    # SSE 路径：纯 JSON 可序列化
    text = json.dumps({"type": "context_hits", "data": payload}, ensure_ascii=False)
    assert "context_hits" in text and payload["tool_calls"][0]["args"] != "x"  # 密码已脱敏
    assert "password" in payload["tool_calls"][0]["args"]  # 键名保留、值脱敏

    # 非流式路径：Pydantic 校验通过且字段齐全
    resp = ChatResponse(answer="a", sources=[], context_hits=payload)
    tc = resp.context_hits.tool_calls[0]
    assert tc.name == "t" and tc.status == "degraded" and tc.attempts == 2
    assert tc.error_code == "tool_failure" and tc.public_message
    assert tc.hits.terms[0].term == "GMV" and tc.hits.docs[0].source == "ops.md"
    assert resp.model_dump()["context_hits"]["summary"]["terms"] == 1


# ========== 安全与异常边界（外部 CR 回归） ==========


def test_unknown_mcp_tool_shows_keys_only():
    """白名单外工具（动态 MCP）只展示参数名列表，不展示值——OpenSpec 安全约定。"""
    args = {"query": "SELECT private_data", "tenant": "acme"}
    text = summarize_args("mcp_some_server_tool", args)
    assert "query" in text and "tenant" in text  # 参数名可见
    assert "private_data" not in text and "acme" not in text  # 值不可见

    # 本地白名单工具：脱敏后正常展示值
    local = summarize_args("execute_sql", {"sql": "SELECT 1"})
    assert "SELECT 1" in local

    # 非法结构（str 而非 dict）也不泄漏原文
    assert summarize_args("mcp_x", "not-a-dict") == "（参数不可解析）"

    # 本地工具序列化失败 fail closed：固定占位文本，绝不回退 str(args) 原文
    class _Bad:
        def __init__(self):
            self.ok = 1

    assert summarize_args("execute_sql", {"x": _Bad()}) != ""  # 可序列化（default=str）
    bad_obj = object()
    text = summarize_args("execute_sql", {"cycle": bad_obj})
    assert isinstance(text, str)  # 任何输入都有安全输出


async def test_middleware_cancellation_records_and_reraises():
    """CancelledError 穿出 safe_tool_execute（只捕 Exception）时：轨迹记 cancelled、异常原样上抛、
    不被 UnboundLocalError 覆盖。"""
    import asyncio

    from app.agents.middlewares import ToolRuntimeMiddleware
    from app.agents.tool_runtime import reset_tool_runtime_state

    class _Request:
        def __init__(self, name, args, call_id):
            self.tool_call = {"name": name, "args": args, "id": call_id}

    async def cancelled_handler(request):
        raise asyncio.CancelledError()

    mw = ToolRuntimeMiddleware()
    reset_tool_runtime_state()
    with use_context_trace():
        with pytest.raises(asyncio.CancelledError):  # 原始取消异常上抛，未被覆盖
            await mw.awrap_tool_call(_Request("execute_sql", {"sql": "SELECT 1"}, "c1"), cancelled_handler)
        payload = context_hits_payload()

    assert payload["tool_calls"][0]["status"] == "cancelled"
    assert payload["tool_calls"][0]["error_code"] == "cancelled"


def test_doc_hit_key_uses_content_hash_not_rank():
    """缺 chunk_index 时 hit_key 用内容哈希：同来源两个不同片段不会碰撞为同一个键。"""
    with use_context_trace():
        c1 = record_tool_start("c1", "query_internal_docs", {})
        with use_active_tool_trace(c1):
            record_doc_hits(
                [
                    {"source": "ops.md", "title": "t", "content": "片段甲"},
                    {"source": "ops.md", "title": "t", "content": "片段乙"},
                ]
            )
        payload = context_hits_payload()

    docs = payload["tool_calls"][0]["hits"]["docs"]
    assert len(docs) == 2
    assert docs[0]["hit_key"] != docs[1]["hit_key"]  # 内容不同 → 键不同
    assert payload["summary"]["docs"] == 2  # 摘要不被错误去重为 1


async def test_skills_to_mcp_override_chain_records_once():
    """真实 middleware 组合链：SkillsMiddleware 动态接管（request.override）后，
    ToolRuntimeMiddleware 仍只记录一次轨迹，且 MCP 工具名走 keys-only 脱敏。"""
    from langchain_core.tools import tool as lc_tool

    from app.agents.middlewares import ToolRuntimeMiddleware
    from app.agents.tool_runtime import reset_tool_runtime_state

    recorded_calls: list[str] = []

    @lc_tool
    def mcp_fake_query(query: str, tenant: str = "") -> str:
        """fake MCP 工具：记录被调用的参数。"""
        recorded_calls.append(f"{query}|{tenant}")
        return "mcp 结果"

    class _FakeSkillsService:
        pass

    class _Request:
        def __init__(self, name, args, call_id):
            self.tool_call = {"name": name, "args": args, "id": call_id}

    # 构造 Skills 动态接管的 handler：模拟 override 后执行 MCP 工具
    runtime_mw = ToolRuntimeMiddleware()

    async def base_handler(request):
        return type("M", (), {"content": "fallback"})()

    async def mcp_handler(request):
        # 模拟 wrap_tool_call 中 override(tool=...) 的接管效果：真实执行 MCP 工具
        out = await mcp_fake_query.ainvoke(request.tool_call["args"])
        return type("M", (), {"content": out})()

    reset_tool_runtime_state()
    request = _Request("mcp_fake_query", {"query": "secret-q", "tenant": "acme"}, "call_mcp_1")
    with use_context_trace():
        # 模拟真实链：Runtime 外层 → 内层是 override 后的 handler
        await runtime_mw.awrap_tool_call(request, mcp_handler)
        payload = context_hits_payload()

    assert recorded_calls == ["secret-q|acme"]  # 工具只执行一次
    assert len(payload["tool_calls"]) == 1  # 轨迹只记录一次
    tc = payload["tool_calls"][0]
    assert tc["status"] == "success"
    assert "secret-q" not in tc["args"] and "acme" not in tc["args"]  # keys-only
    assert "query" in tc["args"] and "tenant" in tc["args"]
