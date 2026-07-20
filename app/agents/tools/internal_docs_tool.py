from typing import Any, Dict

from langchain.tools import tool


async def _search_docs(retriever: Any, query: str, metadata_filters: Dict[str, Any]):
    if hasattr(retriever, "retrieve_multi_query"):
        return await retriever.retrieve_multi_query(
            query,
            top_k=3,
            metadata_filters=metadata_filters,
        )

    if hasattr(retriever, "search"):
        return await retriever.search(
            query,
            top_k=3,
            metadata_filters=metadata_filters,
        )

    raise TypeError("retriever must provide retrieve_multi_query() or search()")


def create_docs_tool(retriever: Any):
    @tool
    async def query_internal_docs(
        query: str,
        doc_type: str = "",
        title: str = "",
        title_contains: str = "",
        source: str = "",
        section_path: str = "",
        section_path_contains: str = "",
        sheet_name: str = "",
        timestamp: int = 0,
        ingested_at_from: int = 0,
        ingested_at_to: int = 0,
        timestamp_from: int = 0,
        timestamp_to: int = 0,
    ) -> str:
        """Query the internal document knowledge base."""
        metadata_filters = {
            "doc_type": doc_type or None,
            "title": title or None,
            "title_contains": title_contains or None,
            "source": source or None,
            "section_path": section_path or None,
            "section_path_contains": section_path_contains or None,
            "sheet_name": sheet_name or None,
            "timestamp": timestamp or None,
            "ingested_at_from": ingested_at_from or None,
            "ingested_at_to": ingested_at_to or None,
            "timestamp_from": timestamp_from or None,
            "timestamp_to": timestamp_to or None,
        }

        docs = await _search_docs(retriever, query, metadata_filters)
        if not docs:
            return "未找到相关文档"

        result = []
        for i, doc in enumerate(docs, 1):
            metadata = doc.get("metadata", {})
            meta_parts = [f"source={metadata.get('source', 'unknown')}"]
            if metadata.get("title"):
                meta_parts.append(f"title={metadata['title']}")
            if metadata.get("section_path"):
                meta_parts.append(f"section_path={metadata['section_path']}")
            if metadata.get("sheet_name"):
                meta_parts.append(f"sheet_name={metadata['sheet_name']}")
            if metadata.get("doc_type"):
                meta_parts.append(f"doc_type={metadata['doc_type']}")
            if metadata.get("timestamp"):
                meta_parts.append(f"timestamp={metadata['timestamp']}")
            result.append(f"【文档 {i} | {', '.join(meta_parts)}】\n{doc['content']}\n")

        return "\n".join(result)

    return query_internal_docs
