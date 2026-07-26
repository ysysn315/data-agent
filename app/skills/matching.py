"""Skills 语义匹配 —— embedding 向量召回 + jieba 关键词回退

把 auto_match 模式下"按用户输入挑技能"的打分逻辑从 service 里抽出来单独承载。

策略（strategy=auto）判定：
- settings 的 embedding 配置可用（embedding_api_key 非空，或 provider=bge）→ 向量召回
- 否则回退 jieba 关键词匹配（与 service v1 的 match_skills_by_query 打分逐字节兼容）

向量召回：每个技能的 `name + description` embed 后按 slug 惰性缓存进内存（dict），
查询 embed 后与候选技能向量算余弦、取 top-k；技能增/删/改由 SkillService 调
`invalidate(slug)` 失效对应条目，下次匹配重算。

失败降级：任何一次 embedding 调用抛错（网络/配额）→ logger.warning + 本次请求
回退 jieba；**不缓存失败状态**（已成功算出的技能向量保留，失败的下次再试）。
匹配是"增强"而非关键路径，失败降级不打断上层 auto_match（见 IMPLEMENTATION-matching.md ④）。
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Protocol, Sequence

import jieba
from loguru import logger

if TYPE_CHECKING:  # 仅类型提示，避免运行时强依赖 settings/skill 具体类型
    from app.core.settings import Settings

# 单文本 embed 函数签名：async (text) -> 向量
EmbedFn = Callable[[str], Awaitable[list[float]]]


class _SkillLike(Protocol):
    """匹配只需要技能的这几个字段（不绑定 Skill 具体类型，便于测试注入桩）"""

    slug: str
    name: str
    description: str


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（纯 Python，无 numpy 依赖）

    实现对齐 SQLBot backend/apps/datasource/embedding/utils.py:cosine_similarity；
    维度不一致或零向量一律返回 0.0（降级而非抛错）。
    """
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class SkillMatcher:
    """技能语义匹配器：embedding 向量召回，失败/未配置回退 jieba 关键词。

    - `settings`：用于 auto 策略判定；None 时 auto 恒判为不可用（回退 jieba）。
    - `embed_fn`：可注入的单文本 embed 函数；None 时首次使用惰性从
      `app.rag.embeddings.EmbeddingService` 取（离线测试注入假函数即可完全不碰真实 API）。
    - `strategy`：`auto`（默认，按配置判定）/ `embedding`（强制向量）/ `jieba`（强制关键词）。
    """

    def __init__(
        self,
        settings: Optional["Settings"] = None,
        embed_fn: Optional[EmbedFn] = None,
        strategy: str = "auto",
    ):
        self.settings = settings
        self.strategy = strategy
        self._embed_fn = embed_fn
        self._embedding_service = None  # 惰性构建的 EmbeddingService（默认 embed 源）
        # slug -> 技能 name+description 的向量缓存（惰性构建，按 slug 失效）
        self._vector_cache: dict[str, list[float]] = {}

    # ========== 策略判定 ==========

    def use_embedding(self) -> bool:
        """本匹配器是否走 embedding 路径（auto 策略下看 settings 的 embedding 配置）"""
        if self.strategy == "jieba":
            return False
        if self.strategy == "embedding":
            return True
        # auto：embedding_api_key 非空，或本地 bge 无需 key，均视为可用
        if self.settings is None:
            return False
        if getattr(self.settings, "embedding_provider", None) == "bge":
            return True
        return bool(getattr(self.settings, "embedding_api_key", ""))

    # ========== 缓存失效钩子（供 SkillService 增/删/改调用） ==========

    def invalidate(self, slug: str) -> None:
        """失效单个技能的向量缓存（改名/改描述/删除后调用），下次匹配重算"""
        self._vector_cache.pop(slug, None)

    def invalidate_all(self) -> None:
        """清空全部向量缓存"""
        self._vector_cache.clear()

    # ========== 分词（语义匹配的关键词回退共用；service._tokenize 委托到此） ==========

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """jieba 分词（中英文皆可），过滤空白与单字符标点

        注意：CJK 单字（如 "图"）`isalnum()` 为真会被保留，与 service v1 口径一致。
        """
        return {
            token for token in jieba.lcut(text.lower())
            if token.strip() and (len(token) > 1 or token.isalnum())
        }

    # ========== 主入口 ==========

    async def match(
        self,
        query: str,
        skills: Sequence[_SkillLike],
        top_k: int = 3,
    ) -> list:
        """在候选 `skills` 中按 `query` 召回 top-k（策略 auto/embedding/jieba）"""
        if self.use_embedding():
            try:
                return await self._embedding_match(query, skills, top_k)
            except Exception as e:  # 网络/配额/维度等任何失败：本次回退 jieba，不缓存失败
                logger.warning(f"embedding 语义召回失败，本次回退 jieba 关键词匹配: {e}")
        return self._jieba_match(query, skills, top_k)

    # ========== jieba 关键词（与 service v1 match_skills_by_query 打分逐字节兼容） ==========

    def _jieba_match(self, query: str, skills: Sequence[_SkillLike], top_k: int) -> list:
        query_lower = query.lower()
        query_tokens = self._tokenize(query)
        scored_skills = []

        for skill in skills:
            score = 0.0
            # slug 直接出现在查询里（如用户明确点名）
            if skill.slug in query_lower:
                score += 10
            # name 词元重叠
            score += 2 * len(self._tokenize(skill.name) & query_tokens)
            # description 词元重叠
            score += len(self._tokenize(skill.description) & query_tokens)

            if score > 0:
                scored_skills.append((skill, score))

        scored_skills.sort(key=lambda x: x[1], reverse=True)
        return [skill for skill, _ in scored_skills[:top_k]]

    # ========== embedding 向量召回 ==========

    async def _embedding_match(self, query: str, skills: Sequence[_SkillLike], top_k: int) -> list:
        # 1. 惰性补齐候选技能的向量缓存（仅未缓存的才 embed；失败向上抛，由 match 兜底回退）
        for skill in skills:
            if skill.slug not in self._vector_cache:
                text = f"{skill.name} {skill.description}".strip()
                # 先算再写：embed 抛错则不落缓存（不缓存失败状态）
                self._vector_cache[skill.slug] = await self._embed(text)

        # 2. 查询向量（每次都算，查询是变化的、不缓存）
        query_vec = await self._embed(query)

        # 3. 余弦排序取 top-k（纯召回，不设阈值：技能数少、上层 top_k 已封顶噪声）
        scored = [
            (skill, cosine_similarity(query_vec, self._vector_cache[skill.slug]))
            for skill in skills
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [skill for skill, _ in scored[:top_k]]

    async def _embed(self, text: str) -> list[float]:
        """取单文本向量：优先注入的 embed_fn，否则惰性构建 EmbeddingService"""
        if self._embed_fn is None:
            from app.rag.embeddings import EmbeddingService

            if self._embedding_service is None:
                self._embedding_service = EmbeddingService(self.settings)
            self._embed_fn = self._embedding_service.embed_text
        return await self._embed_fn(text)
