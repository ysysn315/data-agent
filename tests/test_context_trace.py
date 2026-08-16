"""context_trace 记录器测试（基础节：生命周期/脱敏/上限/负载）。

记录点接入、middleware 轨迹、SSE 下发的测试在后续 commit 随接线补齐；
本文件先锁死记录器自身的语义契约。
"""

from __future__ import annotations

from app.agents.context_trace import (
    ARGS_MAX_CHARS,
    MAX_HITS_PER_TYPE,
    MAX_TOOL_CALLS,
    ExampleHit,
    TermHit,
    context_hits_payload,
    current_context_trace,
    finish_tool_call,
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
    text = summarize_args(args)
    assert "hunter2" not in text and "sk-123" not in text and "abc" not in text
    assert "***" in text
    assert "SELECT 1" in text and "ok" in text


def test_summarize_args_redacts_dsn_and_bearer_strings():
    """字符串级脱敏：DSN 密码段 / Bearer token / Authorization 值。"""
    text = summarize_args({"dsn": "postgres://user:s3cret@db.host:5432/prod"})
    assert "s3cret" not in text and "db.host" in text

    text = summarize_args({"auth": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"})
    assert "eyJhbGciOiJIUzI1NiJ9" not in text and "Bearer ***" in text

    text = summarize_args({"headers": {"Authorization": "Bearer abc.def"}})
    assert "abc.def" not in text


def test_summarize_args_truncates_after_redaction():
    """截断在脱敏之后：长文本截到 ARGS_MAX_CHARS，且敏感内容不在前缀里。"""
    args = {"log": "x" * 500, "secret": "leak-me"}
    text = summarize_args(args)
    assert len(text) == ARGS_MAX_CHARS + 1  # 截断 + 省略号
    assert text.endswith("…")
    assert "leak-me" not in text  # 脱敏先于截断


# ========== 错误安全映射 ==========


def test_finish_tool_call_maps_error_safely():
    """失败状态 → 稳定 error_code + 独立中文 public_message；不涉及原始异常。"""
    with use_context_trace():
        call = record_tool_start("c1", "execute_sql", {"sql": "SELECT 1"})
        finish_tool_call(call, status="timeout", attempts=2)
        assert call.error_code == "timeout" and "超时" in call.public_message

        call2 = record_tool_start("c2", "t", {})
        finish_tool_call(call2, status="circuit_open")
        assert call2.error_code == "circuit_open"

        call3 = record_tool_start("c3", "t", {})
        finish_tool_call(call3, status="weird_unknown")  # 未知状态
        assert call3.error_code == "unknown"

        call4 = record_tool_start("c4", "t", {})
        finish_tool_call(call4, status="success")
        assert call4.error_code is None and call4.public_message is None


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
