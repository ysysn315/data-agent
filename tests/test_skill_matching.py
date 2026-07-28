"""SkillMatcher 测试：auto 策略判定 / embedding 语义召回 / jieba 回退 / 缓存失效

离线测试注入确定性假 embed（按关键词构造近似正交向量），**不碰真实 embedding API**。
"""

from types import SimpleNamespace

import pytest

from app.skills.matching import SkillMatcher, cosine_similarity
from app.skills.repository import InMemorySkillRepository
from app.skills.service import SkillService
from tests.conftest import make_skill_md

# ---- 确定性假 embedding：关键词 -> 轴，命中即在该轴 +1（近似正交） ----
_AXES = [
    ["图", "可视化", "图表", "走势", "趋势", "画", "chart", "visualization", "plot"],  # 轴0 可视化
    ["sql", "语句", "generation", "生成"],  # 轴1 SQL 生成
    ["schema", "结构", "字段", "检索", "retrieval", "表结构"],  # 轴2 schema 检索
    ["sqlite", "执行", "query", "运行"],  # 轴3 执行
]


def _fake_vector(text: str) -> list[float]:
    low = text.lower()
    vec = [0.0] * len(_AXES)
    for i, kws in enumerate(_AXES):
        for kw in kws:
            if kw in low:
                vec[i] += 1.0
    if not any(vec):
        vec = [1e-6] * len(_AXES)  # 全零兜底，避免余弦退化
    return vec


def _make_embed(sink: list | None = None):
    """构造假 embed 函数；传入 sink 时记录每次被 embed 的文本（供计数断言）"""

    async def _embed(text: str) -> list[float]:
        if sink is not None:
            sink.append(text)
        return _fake_vector(text)

    return _embed


# ========== auto 策略判定表 ==========


def test_use_embedding_decision_table():
    NS = SimpleNamespace
    # auto：settings 缺失 / key 为空且非 bge -> 关键词
    assert SkillMatcher(settings=None).use_embedding() is False
    assert SkillMatcher(settings=NS(embedding_provider="openai", embedding_api_key="")).use_embedding() is False
    # auto：key 非空 -> 向量
    assert SkillMatcher(settings=NS(embedding_provider="openai", embedding_api_key="sk-x")).use_embedding() is True
    # auto：本地 bge 无需 key -> 向量
    assert SkillMatcher(settings=NS(embedding_provider="bge", embedding_api_key="")).use_embedding() is True
    # 显式 strategy 覆盖 settings 判定
    forced_on = SkillMatcher(settings=NS(embedding_provider="openai", embedding_api_key=""), strategy="embedding")
    forced_off = SkillMatcher(settings=NS(embedding_provider="bge", embedding_api_key=""), strategy="jieba")
    assert forced_on.use_embedding() is True
    assert forced_off.use_embedding() is False


def test_cosine_similarity_basics():
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)  # 正交
    assert cosine_similarity([0, 0], [1, 1]) == 0.0  # 零向量降级
    assert cosine_similarity([1, 2, 3], [1, 2]) == 0.0  # 维度不一致降级


# ========== embedding 语义召回 优于 jieba 关键词 ==========


async def test_embedding_beats_keyword_on_semantic_query(skill_service):
    """ "帮我画个销售走势的图"：jieba 与 data-visualization 词元零交集（关键词失配），
    但语义上就是要画图 —— 向量召回应把 data-visualization 排第一。"""
    skills = await skill_service.list_skills(enabled_only=True)
    query = "帮我画个销售走势的图"

    # jieba：关键词失配，data-visualization 落选
    jieba_matcher = SkillMatcher(strategy="jieba")
    jieba_hits = [s.slug for s in await jieba_matcher.match(query, skills, top_k=3)]
    assert "data-visualization" not in jieba_hits

    # embedding：语义命中，data-visualization 排第一
    embed_matcher = SkillMatcher(embed_fn=_make_embed(), strategy="embedding")
    embed_hits = [s.slug for s in await embed_matcher.match(query, skills, top_k=3)]
    assert embed_hits[0] == "data-visualization"


# ========== 无 embedding 配置 -> 回退 jieba，且从不调用 embed ==========


async def test_auto_without_config_uses_jieba(skill_service):
    skills = await skill_service.list_skills(enabled_only=True)
    settings = SimpleNamespace(embedding_provider="openai", embedding_api_key="")
    sink: list[str] = []
    matcher = SkillMatcher(settings=settings, embed_fn=_make_embed(sink), strategy="auto")

    assert matcher.use_embedding() is False
    hits = [s.slug for s in await matcher.match("查询数据库表结构", skills, top_k=3)]
    assert "schema-retrieval" in hits  # jieba 中文命中
    assert sink == []  # embed 从未被调用


# ========== embedding 抛异常 -> 回退 jieba，不崩，不缓存失败 ==========


async def test_embedding_exception_falls_back_to_jieba(skill_service, loguru_capture):
    skills = await skill_service.list_skills(enabled_only=True)

    async def boom_embed(text: str):
        raise RuntimeError("配额用尽")

    matcher = SkillMatcher(embed_fn=boom_embed, strategy="embedding")
    hits = [s.slug for s in await matcher.match("查询数据库表结构", skills, top_k=3)]

    assert "schema-retrieval" in hits  # 回退 jieba 命中，未崩
    assert matcher._vector_cache == {}  # 失败不落缓存，下次可重试
    assert any("回退 jieba" in r for r in loguru_capture)  # 有 warning


# ========== 向量缓存：第二次匹配不再 embed 已缓存技能 ==========


async def test_vector_cache_reuses_skill_embeddings(skill_service):
    skills = await skill_service.list_skills(enabled_only=True)
    sink: list[str] = []
    matcher = SkillMatcher(embed_fn=_make_embed(sink), strategy="embedding")

    await matcher.match("帮我画个销售走势的图", skills, top_k=3)
    first = len(sink)
    assert first == len(skills) + 1  # 每个技能各 1 次 + 查询 1 次

    await matcher.match("再来一个关于图表的问题", skills, top_k=3)
    second = len(sink)
    assert second == first + 1  # 技能全部命中缓存，仅新查询被 embed


# ========== update_skill 失效缓存并重算（service 挂钩） ==========


async def test_update_skill_invalidates_cache(tmp_path):
    sink: list[str] = []
    matcher = SkillMatcher(embed_fn=_make_embed(sink), strategy="embedding")
    service = SkillService(
        repository=InMemorySkillRepository(),
        save_dir=tmp_path,
        matcher=matcher,
    )
    await service.create_skill(make_skill_md("alpha"))
    await service.create_skill(make_skill_md("beta"))

    await service.match_skills_by_query("测试", top_k=2)  # 建立 alpha/beta 缓存
    sink.clear()

    await service.update_skill("alpha", make_skill_md("alpha"))  # 只失效 alpha
    await service.match_skills_by_query("测试", top_k=2)

    assert any("alpha" in t for t in sink)  # alpha 被重算
    assert not any("beta" in t for t in sink)  # beta 命中缓存，未重算


async def test_delete_skill_invalidates_cache(tmp_path):
    sink: list[str] = []
    matcher = SkillMatcher(embed_fn=_make_embed(sink), strategy="embedding")
    service = SkillService(
        repository=InMemorySkillRepository(),
        save_dir=tmp_path,
        matcher=matcher,
    )
    await service.create_skill(make_skill_md("gamma"))
    await service.match_skills_by_query("测试", top_k=1)
    assert "gamma" in matcher._vector_cache

    await service.delete_skill("gamma")
    assert "gamma" not in matcher._vector_cache  # 删除后缓存条目被清
