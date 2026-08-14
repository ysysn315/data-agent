"""作用域内实体解析：精确/别名优先，Embedding 增强，失败可解释降级。"""

from __future__ import annotations

import asyncio
import threading
from typing import Callable

from app.graph.entities import (
    EmbedFn,
    EntityCandidate,
    EntityResolution,
    cosine_similarity,
    embedding_hash,
    entity_index_text,
    lexical_candidates,
    maybe_embed,
    normalize_entity_name,
)
from app.graph.scope import GraphScope


class EntityResolver:
    def __init__(
        self,
        repo,
        runner: Callable,
        embed_fn: EmbedFn | None = None,
        index=None,
        top_k: int = 5,
        min_score: float = 0.72,
        merge_margin: float = 0.05,
    ):
        self._repo = repo
        self._run = runner
        self._embed_fn = embed_fn
        self._index = index
        self._top_k = max(1, min(int(top_k), 20))
        self._min_score = float(min_score)
        self._merge_margin = float(merge_margin)
        self._vectors: dict[tuple[str, int, str], list[float]] = {}

    def _run_db(self, factory: Callable):
        """兼容同步 runner 和正在运行的异步事件循环。

        生产环境的 ``run_sync`` 本身提交到后台循环；离线测试注入的 runner 常用
        ``loop.run_until_complete``，不能在 ``asyncio.run`` 内嵌套。此处只在已有
        event loop 时把同步 DB 桥放到短生命周期线程，避免两种 runner 互相污染。
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._run(factory())

        result: list = []
        error: list[BaseException] = []

        def invoke():
            try:
                result.append(self._run(factory()))
            except BaseException as exc:  # noqa: BLE001 - 透传原始 DB 异常
                error.append(exc)

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result[0] if result else None

    async def resolve(self, name: str, scope: GraphScope, entity_type: str | None = None) -> EntityResolution:
        raw = str(name or "").strip()
        normalized = normalize_entity_name(raw)
        if not normalized or self._repo is None:
            return EntityResolution(status="missing", input_name=raw)

        exact = self._run_db(lambda: self._repo.get_by_normalized(scope.key, normalized))
        if exact and self._compatible(exact, entity_type):
            return EntityResolution(status="resolved", input_name=raw, entity=exact, method="exact", score=1.0)

        alias = self._run_db(lambda: self._repo.get_by_alias(scope.key, normalized))
        if alias and self._compatible(alias, entity_type):
            return EntityResolution(status="resolved", input_name=raw, entity=alias, method="alias", score=1.0)

        entities = self._run_db(lambda: self._repo.list_scope(scope.key))
        entities = [entity for entity in entities if self._compatible(entity, entity_type)]
        lexical = lexical_candidates(raw, entities, self._top_k)
        if lexical and lexical[0].score >= 1.0:
            return EntityResolution(
                status="resolved", input_name=raw, entity=lexical[0].entity, method="lexical", score=lexical[0].score
            )

        vector_candidates = await self._vector_candidates(raw, scope, entities)
        candidates = vector_candidates or lexical
        if not candidates:
            return EntityResolution(status="missing", input_name=raw)

        best = candidates[0]
        second = candidates[1].score if len(candidates) > 1 else 0.0
        if best.score >= self._min_score and (len(candidates) == 1 or best.score - second >= self._merge_margin):
            return EntityResolution(
                status="resolved",
                input_name=raw,
                entity=best.entity,
                method=best.method,
                score=best.score,
                candidates=tuple(candidates),
            )
        return EntityResolution(status="ambiguous", input_name=raw, candidates=tuple(candidates))

    def _compatible(self, entity: dict, entity_type: str | None) -> bool:
        return not entity_type or entity.get("entity_type") in (None, "unknown", entity_type)

    async def _vector_candidates(self, query: str, scope: GraphScope, entities: list[dict]) -> list[EntityCandidate]:
        if self._index is not None:
            return await self._index.search(query, entities, scope, top_k=self._top_k)
        try:
            query_vector = await maybe_embed(self._embed_fn, query)
        except Exception:
            # Embedding 是增强路径；网络/配额/维度失败时回退 lexical，不能阻断图查询。
            return []
        if not query_vector:
            return []
        scored: list[EntityCandidate] = []
        for entity in entities:
            text = entity_index_text(entity)
            key = (scope.key, int(entity["id"]), embedding_hash(text))
            vector = self._vectors.get(key)
            if vector is None:
                try:
                    vector = await maybe_embed(self._embed_fn, text)
                except Exception:
                    return []
                if vector:
                    self._vectors[key] = vector
            if not vector:
                continue
            score = cosine_similarity(query_vector, vector)
            if score >= self._min_score * 0.8:
                scored.append(EntityCandidate(entity=entity, score=score, method="embedding"))
        scored.sort(key=lambda item: (-item.score, item.entity.get("canonical_name", "")))
        return scored[: self._top_k]
