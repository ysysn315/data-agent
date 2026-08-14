"""RAG 文档身份字段的共享工具。

检索、来源记录和索引层都使用同一套文档结构；把身份字段的约定放在模块级，
避免调用方依赖 VectorStore 的内部实现。
"""

from typing import Any, Mapping


def document_key(doc: Mapping[str, Any]) -> tuple[Any, Any]:
    """生成跨稠密/BM25 去重键，优先使用稳定的 source+chunk_index。"""
    metadata = doc.get("metadata") or {}
    chunk_index = metadata.get("chunk_index")
    if chunk_index is not None:
        return (metadata.get("source", ""), chunk_index)
    return (metadata.get("source", ""), doc.get("content", ""))


def document_source(doc: Mapping[str, Any]) -> str:
    """提取文档来源，统一处理缺失 metadata 和空白值。"""
    return str((doc.get("metadata") or {}).get("source") or "").strip()
