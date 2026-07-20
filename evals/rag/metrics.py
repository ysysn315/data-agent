from typing import List, Set, Iterable


def hit_at_k(pred: List[str], gold: Set[str], k: int) -> float:
    topk = pred[:k]
    return 1.0 if any(x in gold for x in topk) else 0.0


def recall_at_k(pred: List[str], gold: Set[str], k: int) -> float:
    if not gold:
        return 1.0
    topk = set(pred[:k])
    return len(topk & gold) / len(gold)


def mrr(pred: List[str], gold: Set[str]) -> float:
    for i, x in enumerate(pred, start=1):
        if x in gold:
            return 1.0 / i
    return 0.0


def keyword_recall(answer: str, keywords: Iterable[str]) -> float:
    kws = [k.strip().lower() for k in keywords if k.strip()]
    if not kws:
        return 1.0
    ans = (answer or "").lower()
    hit = sum(1 for k in kws if k in ans)
    return hit / len(kws)


def source_hit(pred_sources: List[str], expected_sources: Iterable[str]) -> float:
    exp = [x.strip().lower() for x in expected_sources if x.strip()]
    if not exp:
        return 1.0
    p = " | ".join((pred_sources or [])).lower()
    hit = sum(1 for e in exp if e in p)
    return hit / len(exp)


import math


def _unique_keep_order(items):
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def precision_at_k(pred, gold, k):
    topk = pred[:k]
    if k <= 0:
        return 0.0
    return len(set(topk) & set(gold)) / k


def average_precision(pred, gold):
    gold = set(gold)
    if not gold:
        return 1.0
    pred = _unique_keep_order(pred)
    hit = 0
    s = 0.0
    for i, d in enumerate(pred, start=1):
        if d in gold:
            hit += 1
            s += hit / i
    return s / len(gold)


def mean_average_precision(list_pred, list_gold):
    if not list_pred:
        return 0.0
    aps = [average_precision(p, g) for p, g in zip(list_pred, list_gold)]
    return sum(aps) / len(aps)


def ndcg_at_k(pred, gold, k):
    gold = set(gold)
    topk = _unique_keep_order(pred)[:k]

    dcg = 0.0
    for i, d in enumerate(topk, start=1):
        rel = 1.0 if d in gold else 0.0
        dcg += rel / math.log2(i + 1)

    ideal_hits = min(len(gold), k)
    idcg = 0.0
    for i in range(1, ideal_hits + 1):
        idcg += 1.0 / math.log2(i + 1)

    return (dcg / idcg) if idcg > 0 else 0.0


import re
import unicodedata


def _normalize_text(text: str) -> str:
    """统一大小写、全半角和空白，降低字符串匹配的脆弱性。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"\s+", "", text)
    return text


def _normalize_source_name(source: str) -> str:
    source = (source or "").replace("\\", "/").split("/")[-1]
    return _normalize_text(source)


def _match_text_rule(text: str, rule) -> bool:
    """
    规则支持两种写法：
    1. "cpu使用率"
    2. {"all_of": [...], "any_of": [...], "none_of": [...]}
    """
    norm_text = _normalize_text(text)

    if isinstance(rule, str):
        return _normalize_text(rule) in norm_text

    if not isinstance(rule, dict):
        return False

    all_of = [_normalize_text(x) for x in rule.get("all_of", []) if x]
    any_of = [_normalize_text(x) for x in rule.get("any_of", []) if x]
    none_of = [_normalize_text(x) for x in rule.get("none_of", []) if x]

    if all_of and not all(x in norm_text for x in all_of):
        return False
    if any_of and not any(x in norm_text for x in any_of):
        return False
    if none_of and any(x in norm_text for x in none_of):
        return False
    return bool(all_of or any_of or isinstance(rule, dict))


def fact_recall_strict(answer: str, expected_facts) -> float:
    """必须事实点命中率。"""
    facts = list(expected_facts or [])
    if not facts:
        return 1.0
    hit = sum(1 for fact in facts if _match_text_rule(answer, fact))
    return hit / len(facts)


def hallucination_score(answer: str, forbidden_claims) -> float:
    """禁忌说法越少，分数越高。"""
    claims = list(forbidden_claims or [])
    if not claims:
        return 1.0
    hit = sum(1 for claim in claims if _match_text_rule(answer, claim))
    return max(0.0, 1.0 - (hit / len(claims)))


def source_recall_strict(
    pred_sources,
    expected_sources_all=None,
    expected_sources_any=None,
) -> float:
    """
    source 召回分成两部分：
    - all: 必须全部命中
    - any: 命中其中一个即可
    """
    pred = {_normalize_source_name(x) for x in (pred_sources or []) if x}
    expected_all = [_normalize_source_name(x) for x in (expected_sources_all or []) if x]
    expected_any = [_normalize_source_name(x) for x in (expected_sources_any or []) if x]

    scores = []
    if expected_all:
        hit = sum(1 for src in expected_all if src in pred)
        scores.append(hit / len(expected_all))
    if expected_any:
        scores.append(1.0 if any(src in pred for src in expected_any) else 0.0)
    if not scores:
        return 1.0
    return sum(scores) / len(scores)


def source_precision_strict(
    pred_sources,
    expected_sources_all=None,
    expected_sources_any=None,
) -> float:
    """只奖励与预期 source 集合一致的引用。"""
    pred = [_normalize_source_name(x) for x in (pred_sources or []) if x]
    pred = _unique_keep_order(pred)
    allowed = {
        _normalize_source_name(x)
        for x in (expected_sources_all or []) + (expected_sources_any or [])
        if x
    }

    if not pred:
        return 1.0 if not allowed else 0.0
    if not allowed:
        return 0.0

    hit = sum(1 for src in pred if src in allowed)
    return hit / len(pred)


def forbidden_source_score(pred_sources, forbidden_sources=None) -> float:
    """命中禁用 source 越少越好。"""
    forbidden = {
        _normalize_source_name(x) for x in (forbidden_sources or []) if x
    }
    pred = {_normalize_source_name(x) for x in (pred_sources or []) if x}

    if not forbidden:
        return 1.0

    hit = sum(1 for src in pred if src in forbidden)
    return max(0.0, 1.0 - (hit / len(forbidden)))


def strict_generation_score(
    fact_score: float,
    source_recall_score: float,
    source_precision_score: float,
    hallucination_score_value: float,
    forbidden_source_score_value: float,
) -> float:
    """
    严格生成总分：
    - 事实点优先
    - 其次看引用是否对
    - 最后惩罚幻觉和噪声 source
    """
    score = (
        fact_score * 0.45
        + source_recall_score * 0.20
        + source_precision_score * 0.15
        + hallucination_score_value * 0.15
        + forbidden_source_score_value * 0.05
    )
    return round(score, 4)
