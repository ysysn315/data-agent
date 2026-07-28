# SQL 业务上下文工具 —— sql-generation 技能声明的门控工具（激活后可见）。
# 一次调用同时返回「命中的业务术语解释」+「top-3 相似历史 SQL 示例」，
# 合并成一个工具（而非术语、示例各一个）是为了省一轮模型调用。
from __future__ import annotations

from langchain_core.tools import tool

from app.text2sql.examples import ExampleStore
from app.text2sql.terminology import TermStore


def create_sql_context_tool(example_store: ExampleStore, term_store: TermStore):
    """创建绑定到示例库/术语库的 SQL 上下文检索工具。

    让模型在生成 SQL 前主动调用，把「按公司口径怎么算」（术语）和「类似问题以前怎么写」
    （示例）一次取回。选择让模型显式调用而非 middleware 静默注入，是为了可观察、可讲解
    （详见 app/text2sql/IMPLEMENTATION-knowledge.md）。
    """

    @tool
    def sql_context_search(question: str) -> str:
        """检索与问题相关的业务术语口径与相似历史 SQL 示例（few-shot 参考）。

        生成 SQL 前建议先调用本工具：命中的术语会给出该指标的统一计算口径，
        相似示例给出可借鉴的写法。请仍以 schema_search 返回的真实表结构为准。

        参数:
            question: 用户的自然语言问题
        """
        term_hits = term_store.match(question)
        examples = example_store.search(question, top_k=3)

        if not term_hits and not examples:
            return "未命中任何业务术语或相似示例。请直接依据 schema_search 返回的 M-Schema 生成 SQL。"

        parts: list[str] = []

        if term_hits:
            lines = ["## 命中的业务术语（按此口径计算指标）"]
            for t in term_hits:
                syn = f"（同义词：{'、'.join(t['synonyms'])}）" if t.get("synonyms") else ""
                lines.append(f"- {t['term']}{syn}：{t['definition']}")
                if t.get("sql_hint"):
                    lines.append(f"  SQL 口径：{t['sql_hint']}")
            parts.append("\n".join(lines))
        else:
            parts.append("## 命中的业务术语\n（无）")

        if examples:
            lines = ["## 相似历史 SQL 示例（few-shot 参考，请按当前问题调整）"]
            for i, ex in enumerate(examples, 1):
                mark = "✓已验证" if ex.get("verified") else "未验证"
                lines.append(f"{i}. [{mark}] 问题：{ex['question']}")
                lines.append(f"   SQL：{ex['sql']}")
            parts.append("\n".join(lines))
        else:
            parts.append("## 相似历史 SQL 示例\n（无）")

        return "\n\n".join(parts)

    return sql_context_search
