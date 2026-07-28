"""ReasoningChatOpenAI 单元测试

覆盖 reasoning_content 处理的关键路径（对照前两轮 code-review 的实测验证）：

流式 _convert_chunk_to_generation_chunk（6 场景）：
1. 纯思考帧 → reasoning 进 additional_kwargs，content 为空
2. content+reasoning 同帧 → 不合并，各自保留
3. delta=None 帧 → 返回 None 不崩（父类有 None 防护，子类不得绕过）
4. choices 空帧 → 父类正常处理
5. beta.chat.completions.stream 的 chunk.chunk.choices 包装 → 兼容
6. 多帧 __add__ 累加 → additional_kwargs.reasoning_content 子串正确拼接

非流式 _create_chat_result（4 场景）：
7. content 空 + reasoning_content → 兜底回填 content（AnalysisAgent/eval 不再拿空串）
8. content 非空 + reasoning_content → 不被覆盖
9. reasoning 字段（非 reasoning_content）→ fallback 也能兜底
10. 无 reasoning → 原样

LLMFactory 配置：
11. reasoning_models 命中模型名子串 → 走 ReasoningChatOpenAI
12. 不命中 → 走普通 ChatOpenAI
"""
import pytest
from langchain_core.messages import AIMessageChunk

from app.core.llm import ChatOpenAI, LLMFactory, ReasoningChatOpenAI


@pytest.fixture
def reasoning_llm() -> ReasoningChatOpenAI:
    """推理模型 LLM 实例（不发真实请求，仅测方法）"""
    return ReasoningChatOpenAI(
        model="glm-5.2", api_key="test-key", base_url="http://localhost"
    )


def _convert(llm, chunk: dict):
    """流式转换的薄封装，固定 default_chunk_class=AIMessageChunk"""
    return llm._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)


# ---------- 流式：6 场景 ----------


class TestStreamConvert:
    def test_pure_reasoning_goes_to_additional_kwargs(self, reasoning_llm):
        """场景1：纯思考帧，reasoning 进 additional_kwargs，content 保持空"""
        g = _convert(reasoning_llm, {"choices": [{"delta": {"reasoning_content": "思考中"}}]})
        assert g.message.additional_kwargs.get("reasoning_content") == "思考中"
        assert g.message.content == ""

    def test_content_and_reasoning_not_merged(self, reasoning_llm):
        """场景2：content+reasoning 同帧，不合并，各自保留（核心：不污染 content）"""
        g = _convert(
            reasoning_llm,
            {"choices": [{"delta": {"content": "答案", "reasoning_content": "思考"}}]},
        )
        assert g.message.content == "答案"
        assert g.message.additional_kwargs.get("reasoning_content") == "思考"

    def test_delta_none_returns_none_not_crash(self, reasoning_llm):
        """场景3：delta=None 帧（finish-only/role-only），返回 None 不抛 AttributeError"""
        g = _convert(reasoning_llm, {"choices": [{"delta": None, "finish_reason": "stop"}]})
        assert g is None

    def test_empty_choices_handled(self, reasoning_llm):
        """场景4：choices 空帧，父类正常返回（不崩）"""
        g = _convert(reasoning_llm, {"choices": []})
        assert g is not None
        assert g.message.content == ""

    def test_beta_stream_chunk_wrapper(self, reasoning_llm):
        """场景5：beta.chat.completions.stream 把 choices 包在 chunk.chunk.choices 下"""
        g = _convert(
            reasoning_llm,
            {"chunk": {"choices": [{"delta": {"reasoning_content": "beta思考"}}]}},
        )
        assert g.message.additional_kwargs.get("reasoning_content") == "beta思考"

    def test_reasoning_accumulates_across_chunks(self, reasoning_llm):
        """场景6：多帧 __add__ 累加，reasoning_content 子串正确拼接"""
        g1 = _convert(reasoning_llm, {"choices": [{"delta": {"reasoning_content": "第一步"}}]})
        g2 = _convert(reasoning_llm, {"choices": [{"delta": {"reasoning_content": "第二步"}}]})
        merged = g1 + g2
        assert merged.message.additional_kwargs.get("reasoning_content") == "第一步第二步"


# ---------- 非流式：4 场景 ----------


class TestNonStreamCreateResult:
    def _result(self, llm, message: dict):
        return llm._create_chat_result(
            {"choices": [{"message": {"role": "assistant", **message}, "finish_reason": "stop"}]}
        )

    def test_empty_content_backfilled_by_reasoning(self, reasoning_llm):
        """场景7：content 空 + reasoning_content → 兜底回填（AnalysisAgent/eval 不再拿空串）"""
        r = self._result(reasoning_llm, {"content": "", "reasoning_content": "纯思考"})
        assert r.generations[0].message.content == "纯思考"

    def test_nonempty_content_not_overwritten(self, reasoning_llm):
        """场景8：content 非空 → 不被 reasoning_content 覆盖"""
        r = self._result(reasoning_llm, {"content": "答案", "reasoning_content": "思考"})
        assert r.generations[0].message.content == "答案"

    def test_reasoning_field_fallback(self, reasoning_llm):
        """场景9：reasoning 字段（非 reasoning_content）也能兜底"""
        r = self._result(reasoning_llm, {"content": "", "reasoning": "思考备用字段"})
        assert r.generations[0].message.content == "思考备用字段"

    def test_no_reasoning_unchanged(self, reasoning_llm):
        """场景10：无 reasoning → content 原样"""
        r = self._result(reasoning_llm, {"content": "普通答案"})
        assert r.generations[0].message.content == "普通答案"


# ---------- LLMFactory 配置 ----------


class TestLLMFactoryRouting:
    def test_reasoning_model_uses_subclass(self, monkeypatch):
        """场景11：模型名命中 reasoning_models 子串 → 走 ReasoningChatOpenAI"""
        llm = LLMFactory.create_llm(model="glm-5.2", api_key="k", base_url="http://x")
        assert isinstance(llm, ReasoningChatOpenAI)

    def test_non_reasoning_model_uses_plain(self, monkeypatch):
        """场景12：不命中 → 走普通 ChatOpenAI"""
        llm = LLMFactory.create_llm(model="qwen3-max", api_key="k", base_url="http://x")
        assert type(llm) is ChatOpenAI

    def test_deepseek_reasoner_matched(self, monkeypatch):
        """DeepSeek 官方推理模型名 deepseek-reasoner 命中默认列表（曾因写成 -r1 漏判）"""
        llm = LLMFactory.create_llm(model="deepseek-reasoner", api_key="k", base_url="http://x")
        assert isinstance(llm, ReasoningChatOpenAI)
