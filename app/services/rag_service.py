from typing import Dict, List, Optional

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from loguru import logger

from app.rag.query_rewriter import QueryRewriter


class RAGService:
    def __init__(self, vector_store, llm):
        self.vector_store = vector_store
        self.llm = llm
        self.prompt = ChatPromptTemplate.from_template(
            """
你是一个专业严谨的知识助手。请结合检索到的文档内容回答用户问题。
如果文档里没有答案，请明确说明。

较早轮次摘要：
{summary}

文档信息：
{context}

用户问题：
{question}

回答：
""".strip()
        )
        self.query_rewriter = QueryRewriter(llm) if llm else None
        logger.info("RAG 服务初始化完成")

    async def retrieve(
        self,
        question: str,
        top_k: int = 3,
        history: Optional[List[Dict]] = None,
        metadata_filters: Optional[Dict] = None,
        session_summary: Optional[str] = None,
    ) -> List[Dict]:
        if self.query_rewriter:
            rewritten_query = await self.query_rewriter.process(
                question,
                history=history,
                summary=session_summary,
            )
        else:
            rewritten_query = question

        res = await self.vector_store.search(
            rewritten_query,
            top_k,
            metadata_filters=metadata_filters,
        )
        logger.info(f"文档检索到 {len(res)} 个文档")
        return res

    async def retrieve_multi_query(
        self,
        query: str,
        top_k: int = 3,
        history: Optional[List[Dict]] = None,
        metadata_filters: Optional[Dict] = None,
        session_summary: Optional[str] = None,
    ) -> List[Dict]:
        """Multi-query retrieval with rewrite and expansion."""

        if not self.query_rewriter:
            return await self.vector_store.search(
                query,
                top_k=top_k,
                metadata_filters=metadata_filters,
            )

        queries = await self.query_rewriter.process_with_expansions(
            query,
            history=history,
            summary=session_summary,
        )

        all_docs = []
        seen_content = set()
        for q in queries:
            docs = await self.vector_store.search(
                q,
                top_k=top_k,
                metadata_filters=metadata_filters,
            )
            for doc in docs:
                content_key = doc.get("content", "")[:100]
                if content_key not in seen_content:
                    all_docs.append(doc)
                    seen_content.add(content_key)
        return all_docs[:top_k]

    def format_docs(self, docs: List[Dict]) -> str:
        if not docs:
            return "没有找到相关文档。"

        formatted = []
        for i, chunk in enumerate(docs, 1):
            content = chunk.get("content", "")
            metadata = chunk.get("metadata", {})
            meta_parts = [f"来源: {metadata.get('source', '未知来源')}"]
            if metadata.get("title"):
                meta_parts.append(f"标题: {metadata['title']}")
            if metadata.get("section_path"):
                meta_parts.append(f"章节: {metadata['section_path']}")
            if metadata.get("sheet_name"):
                meta_parts.append(f"工作表: {metadata['sheet_name']}")
            if metadata.get("doc_type"):
                meta_parts.append(f"类型: {metadata['doc_type']}")
            if metadata.get("timestamp"):
                meta_parts.append(f"时间戳: {metadata['timestamp']}")
            formatted.append(f"文档{i}（{'；'.join(meta_parts)}）:\n{content}")
        return "\n\n".join(formatted)

    async def generate_answer(
        self,
        question: str,
        metadata_filters: Optional[Dict] = None,
        history: Optional[List[Dict]] = None,
        session_summary: Optional[str] = None,
    ):
        docs = await self.retrieve_multi_query(
            question,
            history=history,
            metadata_filters=metadata_filters,
            session_summary=session_summary,
        )
        formatted = self.format_docs(docs)
        chain = self.prompt | self.llm | StrOutputParser()
        result = await chain.ainvoke(
            {
                "summary": (session_summary or "无较早轮次摘要"),
                "context": formatted,
                "question": question,
            }
        )
        sources = list(set(doc.get("metadata", {}).get("source", "未知来源") for doc in docs))
        return {"answer": result, "sources": sources}

    async def generate_answer_stream(
        self,
        question: str,
        metadata_filters: Optional[Dict] = None,
        history: Optional[List[Dict]] = None,
        session_summary: Optional[str] = None,
    ):
        try:
            docs = await self.retrieve_multi_query(
                question,
                history=history,
                metadata_filters=metadata_filters,
                session_summary=session_summary,
            )
            formatted = self.format_docs(docs)
            chain = self.prompt | self.llm | StrOutputParser()
            async for chunk in chain.astream(
                {
                    "summary": (session_summary or "无较早轮次摘要"),
                    "context": formatted,
                    "question": question,
                }
            ):
                yield chunk
            logger.info("流式回答完成")
        except Exception as e:  # noqa: BLE001
            logger.error(f"流式生成失败: {str(e)}")
            raise Exception(f"流式生成失败: {str(e)}")
