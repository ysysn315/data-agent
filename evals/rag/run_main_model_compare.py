import asyncio
import math
import time
from statistics import mean

import httpx

from app.core.settings import get_settings
from evals.rag.common import (
    build_rag_for_main_model_eval,
    load_json,
    save_json,
)
from evals.rag.metrics import (
    fact_recall_strict,
    forbidden_source_score,
    hallucination_score,
    keyword_recall,
    source_hit,
    source_precision_strict,
    source_recall_strict,
    strict_generation_score,
)

# 主模型对比切到 60 条正式生成集，覆盖之前的 8 条冒烟集结果。
DATASET_PATH = "evals/rag/datasets/rag_generation_cases_formal_template.json"
REPORT_PATH = "evals/rag/reports/main_model_compare_latest.json"

MAIN_MODELS = [
    "qwen3-max",
    "qwen3.6-plus",
    "qwen3.5-plus",
]

FIXED_REWRITE_MODEL = "qwen-turbo"
FIXED_RERANK_MODEL = "qwen-turbo"
FIXED_ENABLE_HYBRID = True
FIXED_ENABLE_RERANK = True
FIXED_DENSE_TOP_K = 10
PRICING_REGION = "中国内地"
PRICING_SOURCE = "https://help.aliyun.com/zh/model-studio/model-pricing"
OPENAI_COMPAT_CHAT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
OPENAI_COMPAT_MAIN_MODELS = {
    "qwen3.6-plus",
    "qwen3.5-plus",
}
DEFAULT_MAIN_MODEL_SUMMARY = "No earlier summary available."

# China mainland official pricing, checked on 2026-04-20.
MODEL_PRICING_CNY_PER_MILLION = {
    "qwen3-max": [
        {"max_input_tokens": 32_000, "input_price": 2.5, "output_price": 10.0},
        {"max_input_tokens": 128_000, "input_price": 4.0, "output_price": 16.0},
        {"max_input_tokens": 252_000, "input_price": 7.0, "output_price": 28.0},
    ],
    "qwen3.6-plus": [
        {"max_input_tokens": 256_000, "input_price": 2.0, "output_price": 12.0},
        {"max_input_tokens": 1_000_000, "input_price": 8.0, "output_price": 48.0},
    ],
    "qwen3.5-plus": [
        {"max_input_tokens": 128_000, "input_price": 0.8, "output_price": 4.8},
        {"max_input_tokens": 256_000, "input_price": 2.0, "output_price": 12.0},
        {"max_input_tokens": 1_000_000, "input_price": 4.0, "output_price": 24.0},
    ],
}


def percentile(values, p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1))
    return round(ordered[index], 2)


def extract_text_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    text_parts.append(str(text))
            elif item:
                text_parts.append(str(item))
        return "".join(text_parts)
    return str(content or "")


def get_price_tier(model_name: str, input_tokens: int) -> dict:
    tiers = MODEL_PRICING_CNY_PER_MILLION[model_name]
    for tier in tiers:
        if input_tokens <= tier["max_input_tokens"]:
            return tier
    return tiers[-1]


def estimate_cost_cny(model_name: str, input_tokens: int, output_tokens: int) -> float:
    tier = get_price_tier(model_name, input_tokens)
    input_cost = (input_tokens / 1_000_000) * tier["input_price"]
    output_cost = (output_tokens / 1_000_000) * tier["output_price"]
    return round(input_cost + output_cost, 6)


def use_openai_compatible_chat(model_name: str) -> bool:
    return model_name in OPENAI_COMPAT_MAIN_MODELS


def normalize_token_usage(token_usage: dict) -> dict:
    input_tokens = int(
        token_usage.get("input_tokens", token_usage.get("prompt_tokens", 0)) or 0
    )
    output_tokens = int(
        token_usage.get("output_tokens", token_usage.get("completion_tokens", 0)) or 0
    )
    total_tokens = int(
        token_usage.get("total_tokens", input_tokens + output_tokens) or 0
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def build_openai_compatible_messages(messages) -> list[dict]:
    role_map = {
        "system": "system",
        "human": "user",
        "ai": "assistant",
    }
    payload_messages = []
    for message in messages:
        payload_messages.append(
            {
                "role": role_map.get(getattr(message, "type", ""), "user"),
                "content": extract_text_content(getattr(message, "content", "")),
            }
        )
    return payload_messages


def format_http_error(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text

    error = data.get("error")
    if isinstance(error, dict):
        code = error.get("code", "")
        message = error.get("message", "")
        if code or message:
            return f"code: {code} message: {message}".strip()

    code = data.get("code", "")
    message = data.get("message", "")
    if code or message:
        return f"code: {code} message: {message}".strip()

    return response.text


async def invoke_openai_compatible_chat(
    model_name: str,
    api_key: str,
    messages,
) -> dict:
    payload = {
        "model": model_name,
        "messages": build_openai_compatible_messages(messages),
        "temperature": 0.0,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            OPENAI_COMPAT_CHAT_ENDPOINT,
            headers=headers,
            json=payload,
        )

    if response.is_error:
        raise RuntimeError(
            f"status_code: {response.status_code} {format_http_error(response)}".strip()
        )

    data = response.json()
    choices = data.get("choices") or []
    message = (choices[0] or {}).get("message", {}) if choices else {}
    return {
        "answer": extract_text_content(message.get("content", "")),
        "token_usage": normalize_token_usage(data.get("usage", {})),
    }


async def generate_answer_with_main_model_usage(rag_service, question: str) -> dict:
    docs = await rag_service.retrieve_multi_query(question)
    formatted = rag_service.format_docs(docs)
    prompt_value = rag_service.prompt.invoke(
        {
            "summary": "无较早轮次摘要",
            "context": formatted,
            "question": question,
        }
    )
    ai_message = await rag_service.llm.ainvoke(prompt_value.to_messages())
    sources = list(
        set(doc.get("metadata", {}).get("source", "未知来源") for doc in docs)
    )
    token_usage = (getattr(ai_message, "response_metadata", {}) or {}).get("token_usage", {})
    return {
        "answer": extract_text_content(getattr(ai_message, "content", "")),
        "sources": sources,
        "token_usage": token_usage,
    }


async def generate_answer_with_fixed_generation_path(
    rag_service,
    question: str,
    model_name: str,
    api_key: str,
) -> dict:
    docs = await rag_service.retrieve_multi_query(question)
    formatted = rag_service.format_docs(docs)
    prompt_value = rag_service.prompt.invoke(
        {
            "summary": DEFAULT_MAIN_MODEL_SUMMARY,
            "context": formatted,
            "question": question,
        }
    )
    messages = prompt_value.to_messages()

    if use_openai_compatible_chat(model_name):
        generation_result = await invoke_openai_compatible_chat(
            model_name=model_name,
            api_key=api_key,
            messages=messages,
        )
        answer = generation_result["answer"]
        token_usage = generation_result["token_usage"]
    else:
        ai_message = await rag_service.llm.ainvoke(messages)
        answer = extract_text_content(getattr(ai_message, "content", ""))
        token_usage = normalize_token_usage(
            (getattr(ai_message, "response_metadata", {}) or {}).get("token_usage", {})
        )

    sources = list(
        set(doc.get("metadata", {}).get("source", "unknown_source") for doc in docs)
    )
    return {
        "answer": answer,
        "sources": sources,
        "token_usage": token_usage,
    }


async def run_one(model_name: str, cases):
    settings = get_settings()
    rag_service, _ = await build_rag_for_main_model_eval(
        generation_model=model_name,
        rewrite_model=FIXED_REWRITE_MODEL,
        reranker_model=FIXED_RERANK_MODEL,
        enable_hybrid=FIXED_ENABLE_HYBRID,
        enable_rerank=FIXED_ENABLE_RERANK,
        dense_top_k=FIXED_DENSE_TOP_K,
    )

    per_case = []
    kw_list = []
    src_list = []
    fact_list = []
    source_recall_list = []
    source_precision_list = []
    hallucination_list = []
    forbidden_source_list = []
    strict_score_list = []
    latency_list = []
    input_tokens_list = []
    output_tokens_list = []
    total_tokens_list = []
    cost_list = []
    errors = 0

    for c in cases:
        question = c["question"]
        expected_keywords = c.get("expected_keywords", [])
        expected_sources = c.get("expected_sources", [])
        expected_sources_all = c.get("expected_sources_all", expected_sources)
        expected_sources_any = c.get("expected_sources_any", [])
        expected_facts = c.get("expected_facts", [])
        forbidden_claims = c.get("forbidden_claims", [])
        forbidden_sources = c.get("forbidden_sources", [])

        started_at = time.perf_counter()
        try:
            out = await generate_answer_with_fixed_generation_path(
                rag_service=rag_service,
                question=question,
                model_name=model_name,
                api_key=settings.dashscope_api_key,
            )
            answer = out.get("answer", "")
            sources = out.get("sources", [])
            token_usage = out.get("token_usage", {})
            error_message = ""
            status = "success"
        except Exception as e:  # noqa: BLE001
            answer = ""
            sources = []
            token_usage = {}
            error_message = str(e)
            status = "error"
            errors += 1

        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        input_tokens = int(token_usage.get("input_tokens", 0) or 0)
        output_tokens = int(token_usage.get("output_tokens", 0) or 0)
        total_tokens = int(token_usage.get("total_tokens", input_tokens + output_tokens) or 0)
        estimated_cost_cny = estimate_cost_cny(model_name, input_tokens, output_tokens)
        kw = keyword_recall(answer, expected_keywords)
        sh = source_hit(sources, expected_sources_all + expected_sources_any)
        fact_score = fact_recall_strict(answer, expected_facts)
        source_recall_score = source_recall_strict(
            sources,
            expected_sources_all=expected_sources_all,
            expected_sources_any=expected_sources_any,
        )
        source_precision_score = source_precision_strict(
            sources,
            expected_sources_all=expected_sources_all,
            expected_sources_any=expected_sources_any,
        )
        hallucination_score_value = hallucination_score(answer, forbidden_claims)
        forbidden_source_score_value = forbidden_source_score(
            sources,
            forbidden_sources=forbidden_sources,
        )
        strict_score = strict_generation_score(
            fact_score=fact_score,
            source_recall_score=source_recall_score,
            source_precision_score=source_precision_score,
            hallucination_score_value=hallucination_score_value,
            forbidden_source_score_value=forbidden_source_score_value,
        )

        kw_list.append(kw)
        src_list.append(sh)
        fact_list.append(fact_score)
        source_recall_list.append(source_recall_score)
        source_precision_list.append(source_precision_score)
        hallucination_list.append(hallucination_score_value)
        forbidden_source_list.append(forbidden_source_score_value)
        strict_score_list.append(strict_score)
        latency_list.append(latency_ms)
        input_tokens_list.append(input_tokens)
        output_tokens_list.append(output_tokens)
        total_tokens_list.append(total_tokens)
        cost_list.append(estimated_cost_cny)

        per_case.append(
            {
                "id": c["id"],
                "question": question,
                "status": status,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_cny": estimated_cost_cny,
                "expected_keywords": expected_keywords,
                "expected_sources": expected_sources,
                "expected_sources_all": expected_sources_all,
                "expected_sources_any": expected_sources_any,
                "question_type": c.get("question_type", "unknown"),
                "difficulty": c.get("difficulty", "unknown"),
                "pred_sources": sources,
                "keyword_recall": round(kw, 4),
                "source_hit": round(sh, 4),
                "fact_recall": round(fact_score, 4),
                "source_recall_strict": round(source_recall_score, 4),
                "source_precision_strict": round(source_precision_score, 4),
                "hallucination_score": round(hallucination_score_value, 4),
                "forbidden_source_score": round(forbidden_source_score_value, 4),
                "strict_score": round(strict_score, 4),
                "answer_preview": answer[:300],
                "error": error_message,
            }
        )

    return {
        "model_name": model_name,
        "fixed_config": {
            "rewrite_model": FIXED_REWRITE_MODEL,
            "reranker_model": FIXED_RERANK_MODEL,
            "enable_hybrid": FIXED_ENABLE_HYBRID,
            "enable_rerank": FIXED_ENABLE_RERANK,
            "dense_top_k": FIXED_DENSE_TOP_K,
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "milvus_collection": settings.milvus_collection,
            "pricing_region": PRICING_REGION,
            "pricing_source": PRICING_SOURCE,
        },
        "summary": {
            "num_cases": len(cases),
            "keyword_recall": round(mean(kw_list), 4) if kw_list else 0.0,
            "source_hit": round(mean(src_list), 4) if src_list else 0.0,
            "fact_recall": round(mean(fact_list), 4) if fact_list else 0.0,
            "source_recall_strict": round(mean(source_recall_list), 4) if source_recall_list else 0.0,
            "source_precision_strict": round(mean(source_precision_list), 4) if source_precision_list else 0.0,
            "hallucination_score": round(mean(hallucination_list), 4) if hallucination_list else 0.0,
            "forbidden_source_score": round(mean(forbidden_source_list), 4) if forbidden_source_list else 0.0,
            "strict_score": round(mean(strict_score_list), 4) if strict_score_list else 0.0,
            "avg_latency_ms": round(mean(latency_list), 2) if latency_list else 0.0,
            "p50_latency_ms": percentile(latency_list, 0.50),
            "p95_latency_ms": percentile(latency_list, 0.95),
            "p99_latency_ms": percentile(latency_list, 0.99),
            "input_tokens": sum(input_tokens_list),
            "output_tokens": sum(output_tokens_list),
            "total_tokens": sum(total_tokens_list),
            "avg_input_tokens": round(mean(input_tokens_list), 2) if input_tokens_list else 0.0,
            "avg_output_tokens": round(mean(output_tokens_list), 2) if output_tokens_list else 0.0,
            "avg_total_tokens": round(mean(total_tokens_list), 2) if total_tokens_list else 0.0,
            "estimated_cost_cny": round(sum(cost_list), 6),
            "avg_cost_cny": round(mean(cost_list), 6) if cost_list else 0.0,
            "error_rate": round(errors / len(cases), 4) if cases else 0.0,
        },
        "cases": per_case,
    }


async def main():
    cases = load_json(DATASET_PATH)
    results = []

    for model_name in MAIN_MODELS:
        result = await run_one(model_name, cases)
        results.append(result)
        print(model_name, result["summary"])

    leaderboard = sorted(
        results,
        key=lambda item: (
            -item["summary"]["strict_score"],
            -item["summary"]["fact_recall"],
            -item["summary"]["source_recall_strict"],
            -item["summary"]["keyword_recall"],
            -item["summary"]["source_hit"],
            item["summary"]["error_rate"],
            item["summary"]["avg_latency_ms"],
        ),
    )

    report = {
        "dataset_path": DATASET_PATH,
        "models": MAIN_MODELS,
        "leaderboard": leaderboard,
        "best": leaderboard[0] if leaderboard else None,
    }

    save_json(REPORT_PATH, report)
    print("saved:", REPORT_PATH)


if __name__ == "__main__":
    asyncio.run(main())
