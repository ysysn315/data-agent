"""图谱实体规范化、属性合并和轻量消歧。"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Sequence

_SEPARATOR_RE = re.compile(r"[\s\-_—–·./\\]+")
_PUNCT_RE = re.compile(r"[，。；：、（）()【】\[\]{}<>《》“”‘’'\"`~!！?？]+")


def normalize_entity_name(value: str) -> str:
    """用于匹配的稳定键：NFKC、大小写、空白和常见标点归一。"""

    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = _SEPARATOR_RE.sub("", text)
    return _PUNCT_RE.sub("", text)


def _source_rank(source: str) -> int:
    return {
        "reviewed": 4,
        "schema_reviewed": 4,
        "physical_schema": 3,
        "manual": 2,
        "llm": 1,
        "seed": 0,
    }.get((source or "").casefold(), 1)


def merge_attributes(existing: dict[str, Any] | None, incoming: dict[str, Any] | None, source: str) -> dict[str, Any]:
    """合并轻量属性，冲突保留多值，不让低优先级来源覆盖审核值。

    JSON 形态保持对调用方友好：无冲突字段仍是标量，冲突字段变成带来源的
    ``[{"value": ..., "source": ...}]`` 列表。
    """

    result = dict(existing or {})
    rank = _source_rank(source)
    for key, value in (incoming or {}).items():
        if value in (None, "", [], {}):
            continue
        old = result.get(key)
        if old is None:
            result[key] = value
            continue
        if old == value:
            continue
        old_values = old if isinstance(old, list) else [{"value": old, "source": "unknown", "rank": 0}]
        if not isinstance(old_values, list):
            old_values = [old_values]
        values = list(old_values)
        if not any((item.get("value") if isinstance(item, dict) else item) == value for item in values):
            values.append({"value": value, "source": source or "unknown", "rank": rank})
        # 审核/人工来源优先排在前面，保持主值稳定且可解释。
        values.sort(key=lambda item: item.get("rank", 0) if isinstance(item, dict) else 0, reverse=True)
        result[key] = values
    return result


def embedding_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:64]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


@dataclass(frozen=True)
class EntityCandidate:
    entity: dict
    score: float
    method: str


@dataclass(frozen=True)
class EntityResolution:
    status: str
    input_name: str
    entity: dict | None = None
    candidates: tuple[EntityCandidate, ...] = ()
    method: str = ""
    score: float = 0.0


EmbedFn = Callable[[str], Awaitable[Sequence[float]] | Sequence[float]]


async def maybe_embed(embed_fn: EmbedFn | None, text: str) -> list[float] | None:
    if embed_fn is None:
        return None
    result = embed_fn(text)
    if hasattr(result, "__await__"):
        result = await result
    return list(result or [])


def entity_index_text(entity: dict) -> str:
    aliases = " ".join(str(a) for a in entity.get("aliases", []) if a)
    attrs = " ".join(f"{k}:{v}" for k, v in (entity.get("attributes") or {}).items())
    return " ".join(
        part for part in (entity.get("canonical_name", ""), aliases, entity.get("entity_type", ""), attrs) if part
    )


def lexical_candidates(query: str, entities: Iterable[dict], limit: int = 5) -> list[EntityCandidate]:
    """无向量时的可解释回退：规范名/别名子串。"""

    needle = normalize_entity_name(query)
    if not needle:
        return []
    scored: list[EntityCandidate] = []
    for entity in entities:
        names = [entity.get("canonical_name", ""), *(entity.get("aliases") or [])]
        normalized = [normalize_entity_name(name) for name in names if name]
        if any(needle == name for name in normalized):
            score = 1.0
        elif any(needle in name or name in needle for name in normalized):
            score = 0.8
        else:
            continue
        scored.append(EntityCandidate(entity=entity, score=score, method="lexical"))
    scored.sort(key=lambda item: (-item.score, item.entity.get("canonical_name", "")))
    return scored[:limit]
