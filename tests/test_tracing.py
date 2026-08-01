"""Langfuse tracing 接入测试（离线，全程不连 Langfuse）。

覆盖四条零侵入约束：
1. 默认关闭 → 返回 []
2. 启用但缺 key → 返回 []
3. 启用 + 假 key（monkeypatch 掉构造函数避免联网）→ 惰性单例，两次调用同一实例，只构造一次
4. 构造失败 → 降级为 []
另外用假模型驱动 ChatAgent 跑一轮，确认 callbacks 注入不炸且回调确被触发。
"""

from typing import List

import pytest
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.core import tracing
from app.core.settings import settings


@pytest.fixture(autouse=True)
def _reset_cache():
    """每个用例前后清空进程内单例，隔离缓存污染。"""
    tracing.reset_langfuse_callbacks()
    yield
    tracing.reset_langfuse_callbacks()


def _enable_with_fake_keys(monkeypatch):
    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-fake")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-fake")
    monkeypatch.setattr(settings, "langfuse_host", "https://langfuse.local")


def test_disabled_by_default_returns_empty():
    # 默认 settings.langfuse_enabled=False
    assert tracing.get_langfuse_callbacks() == []


def test_enabled_without_key_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "langfuse_enabled", True)
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    assert tracing.get_langfuse_callbacks() == []


def test_enabled_returns_cached_singleton(monkeypatch):
    """启用 + 假 key：monkeypatch 掉 Langfuse/CallbackHandler 构造避免联网，验证单例。"""
    import langfuse
    import langfuse.langchain

    class FakeHandler(BaseCallbackHandler):
        pass

    constructed = {"client": 0, "handler": 0}

    def fake_langfuse(**kwargs):
        constructed["client"] += 1
        # 凭证/host 应如实透传
        assert kwargs["public_key"] == "pk-fake"
        assert kwargs["secret_key"] == "sk-fake"
        assert kwargs["host"] == "https://langfuse.local"
        return object()

    def fake_handler():
        constructed["handler"] += 1
        return FakeHandler()

    monkeypatch.setattr(langfuse, "Langfuse", fake_langfuse)
    monkeypatch.setattr(langfuse.langchain, "CallbackHandler", fake_handler)
    _enable_with_fake_keys(monkeypatch)

    first = tracing.get_langfuse_callbacks()
    second = tracing.get_langfuse_callbacks()

    assert len(first) == 1
    assert isinstance(first[0], FakeHandler)
    assert first is second  # 同一列表实例（单例缓存）
    assert first[0] is second[0]  # 同一 handler 实例
    assert constructed == {"client": 1, "handler": 1}  # 惰性：只构造一次


def test_build_failure_degrades_to_empty(monkeypatch):
    """构造 Langfuse 客户端抛错时降级为 []，绝不影响对话主流程。"""
    import langfuse

    def boom(**kwargs):
        raise RuntimeError("模拟 Langfuse 初始化失败")

    monkeypatch.setattr(langfuse, "Langfuse", boom)
    _enable_with_fake_keys(monkeypatch)

    assert tracing.get_langfuse_callbacks() == []


class _FakeModel(BaseChatModel):
    """按脚本吐消息的假模型（对齐 test_skills_middleware.FakeToolCallingModel）。"""

    scripted: List[AIMessage]
    step: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        msg = self.scripted[min(self.step, len(self.scripted) - 1)]
        object.__setattr__(self, "step", self.step + 1)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs) -> "_FakeModel":
        return self


async def test_chat_agent_injects_callbacks_without_crash(monkeypatch):
    """假模型跑一轮，注入一个真实回调，确认注入不炸且回调确被 graph 触发。"""
    from app.agents import chat_agent as chat_agent_module

    class RecordingHandler(BaseCallbackHandler):
        def __init__(self) -> None:
            self.events = 0

        def on_chain_start(self, *args, **kwargs) -> None:
            self.events += 1

        def on_llm_start(self, *args, **kwargs) -> None:
            self.events += 1

        def on_chat_model_start(self, *args, **kwargs) -> None:
            self.events += 1

    handler = RecordingHandler()
    monkeypatch.setattr(chat_agent_module, "get_langfuse_callbacks", lambda: [handler])

    model = _FakeModel(scripted=[AIMessage(content="二")])
    agent = chat_agent_module.ChatAgent(llm=model, tools=[])
    answer = await agent.chat("一加一等于几")

    assert answer == "二"
    assert handler.events > 0  # 注入的回调确实被 graph 执行链触发
