"""知识图谱 - LLM 三元组抽取（轻量版）

参考 Yuxi 的 LLM 图谱抽取器（yuxi-reference/backend/package/yuxi/knowledge/graphs/
extractors/llm.py：实体/关系/属性三层 JSON Schema + json_repair 兜底修复），本项目
收敛为最小三元组形态：只要扁平的 (subject, predicate, object) 数组，不建模实体
类型（label）与属性（attributes）——演示图谱用不上，砍掉后 prompt 与解析都简一半。

解析容错不引 json_repair 依赖，用「剥栅栏 + 括号截取重试」覆盖常见坏输出：
- ```json / ``` 代码栅栏包裹
- JSON 数组前后夹带说明文字（"好的，结果如下：[...] 以上"）
- 顶层被对象包了一层（{"triples": [...]}，括号截取会命中内层数组）
- 完全不是 JSON → 返回 []（抽取是尽力而为，坏输出告警但不炸掉调用方）
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from loguru import logger

# 中文抽取 prompt：JSON 数组输出 + 短名词/动词短语 + 拒绝编造。
# 大括号已按 str.format 转义（{{ }}），只有 {text} 是占位符。
TRIPLE_EXTRACTION_PROMPT = """你是知识图谱构建助手。请从下面的文本中抽取事实三元组。

要求：
1. 只输出一个 JSON 数组，不要输出任何解释或多余文字
2. 数组元素格式：{{"subject": "主语", "predicate": "谓词", "object": "宾语"}}
3. 主语和宾语必须是文本中出现的短名词短语（实体、指标、字段等），谓词是简短的动词或动词短语（如：属于、包含、计算自、统计自）
4. 禁止编造：只抽取文本明确表达的事实，文本中没有的实体或关系一律不要输出
5. 文本中抽不出任何三元组时，输出 []

文本：
{text}
"""


@dataclass
class Triple:
    """一条三元组：主语 -[谓词]-> 宾语；source 标记来源（seed/manual/llm）。"""

    subject: str
    predicate: str
    object: str
    source: str = "manual"

    def to_dict(self) -> dict:
        return asdict(self)


def extract_triples(text: str, llm, source: str = "llm") -> list[Triple]:
    """调用 LLM 从文本抽取三元组。空白文本直接返回 []，不浪费一次调用。

    Args:
        text: 待抽取的文本
        llm: langchain 聊天模型（只依赖 .invoke(str) 协议，测试注入假模型即可离线）
        source: 写入 Triple.source 的来源标记
    """
    if not text or not text.strip():
        return []

    response = llm.invoke(TRIPLE_EXTRACTION_PROMPT.format(text=text.strip()))
    content = getattr(response, "content", response)
    if isinstance(content, list):
        # 多段消息体（少数模型返回 content parts）：拼成一个字符串再解析
        content = "".join(str(part) for part in content)

    triples: list[Triple] = []
    seen: set[tuple[str, str, str]] = set()
    for item in _parse_json_array(str(content)):
        if not isinstance(item, dict):
            continue
        s = str(item.get("subject") or "").strip()
        p = str(item.get("predicate") or "").strip()
        o = str(item.get("object") or "").strip()
        if not (s and p and o):
            continue  # 缺字段/空值的元素跳过，不因个别坏元素废掉整批
        if (s, p, o) in seen:
            continue  # 批内去重（模型偶发重复输出同一条）
        seen.add((s, p, o))
        triples.append(Triple(subject=s, predicate=p, object=o, source=source))
    return triples


def _parse_json_array(content: str) -> list:
    """容错解析：剥代码栅栏后，从每个 '[' 到最后一个 ']' 依次尝试 json.loads。

    「逐个 '[' 重试」让前置杂文本里的中括号（如"抽取[如下]"）不会挡住真正的数组；
    解析全部失败或结果不是数组 → 告警并返回 []。
    """
    cleaned = content.strip()
    if not cleaned:
        return []
    # 剥 ```json ... ``` / ``` ... ``` 栅栏（栅栏在正文中间时，下面的括号截取仍然有效）
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
    if cleaned.rstrip().endswith("```"):
        cleaned = cleaned.rstrip()[:-3]

    end = cleaned.rfind("]")
    if end != -1:
        for start, ch in enumerate(cleaned):
            if ch != "[" or start >= end:
                continue
            try:
                data = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                return data

    logger.warning(f"三元组抽取输出无法解析为 JSON 数组，已忽略：{content[:200]!r}")
    return []
