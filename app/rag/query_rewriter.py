from typing import List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger


class QueryRewriter:
    """History-aware query rewrite and expansion."""

    def __init__(
        self,
        llm,
        enable_expansion: bool = True,
        enable_rewrite: bool = True,
        max_history_messages: int = 6,
    ):
        self.llm = llm
        self.enable_expansion = enable_expansion
        self.enable_rewrite = enable_rewrite
        self.max_history_messages = max_history_messages

    def _format_history(self, history: Optional[List[dict]]) -> str:
        if not history:
            return "无最近对话"

        recent_history = history[-self.max_history_messages :]
        lines = []
        for msg in recent_history:
            role = "用户" if msg.get("role") == "user" else "助手"
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            lines.append(f"{role}: {content}")
        return "\n".join(lines) if lines else "无最近对话"

    @staticmethod
    def _format_summary(summary: Optional[str]) -> str:
        summary = (summary or "").strip()
        return summary or "无较早轮次摘要"

    async def rewrite(
        self,
        query: str,
        history: Optional[List[dict]] = None,
        summary: Optional[str] = None,
    ) -> str:
        """Rewrite the current query into a self-contained query."""

        if not self.enable_rewrite:
            return query

        history_text = self._format_history(history)
        summary_text = self._format_summary(summary)
        prompt = f"""你是一个面向检索的查询改写器。请把用户当前问题改写成更清晰、自包含、适合搜索的查询。

规则：
1. 结合较早轮次摘要和最近对话，补全代词、省略信息和缩写含义。
2. 保持用户原意，不要编造历史中不存在的事实。
3. 如果当前问题已经足够自包含，就尽量少改。
4. 只输出改写后的查询，不要解释。

较早轮次摘要：
{summary_text}

最近对话：
{history_text}

当前问题：
{query}

改写后的查询："""

        try:
            messages = [
                SystemMessage(content="你是一个面向检索的查询改写器，只输出自包含查询。"),
                HumanMessage(content=prompt),
            ]
            response = await self.llm.ainvoke(messages)
            rewritten = response.content.strip()
            logger.info(f"[QueryRewriter] 改写: '{query}' -> '{rewritten}'")
            return rewritten or query
        except Exception as e:  # noqa: BLE001
            logger.warning(f"查询改写失败，回退原问题: {e}")
            return query

    async def expand(self, query: str, num_expansions: int = 3) -> List[str]:
        """Expand a rewritten query into related variants."""

        if not self.enable_expansion:
            return [query]

        prompt = f"""你是一个搜索专家。请基于下面这条已经自包含的查询，生成 {num_expansions} 条相关搜索查询。

规则：
1. 可以补充同义词、相关术语和不同表达方式。
2. 不要偏离原始意图。
3. 保留关键实体、指标、告警名、组件名等核心约束。
4. 每行一条查询，不要编号。

自包含查询：
{query}

相关查询："""

        try:
            messages = [
                SystemMessage(content="你是一个搜索查询扩展器，每行输出一条查询。"),
                HumanMessage(content=prompt),
            ]
            response = await self.llm.ainvoke(messages)
            expansions = [line.strip() for line in response.content.strip().split("\n") if line.strip()]
            all_queries = [query] + expansions
            all_queries = list(dict.fromkeys(all_queries))
            logger.info(f"[QueryRewriter] 扩展: '{query}' -> {all_queries}")
            return all_queries[: num_expansions + 1]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"查询扩展失败，回退原查询: {e}")
            return [query]

    async def process(
        self,
        query: str,
        history: Optional[List[dict]] = None,
        summary: Optional[str] = None,
    ) -> str:
        """Rewrite the query with summary and recent history."""

        return await self.rewrite(query, history=history, summary=summary)

    async def process_with_expansions(
        self,
        query: str,
        history: Optional[List[dict]] = None,
        summary: Optional[str] = None,
    ) -> List[str]:
        rewritten = await self.rewrite(query, history=history, summary=summary)
        return await self.expand(rewritten)
