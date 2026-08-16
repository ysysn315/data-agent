"""知识图谱（轻量平台化版）

请求作用域/实体解析 → LLM 三元组抽取（extractor）→ SQLite 持久化 + NetworkX
内存镜像（store）→ 邻居子图/路径/统计（service）。设计取舍与 Yuxi 完整版
（Neo4j+Milvus 双存储）的对照见 IMPLEMENTATION.md。
"""

from app.graph.extractor import Triple, extract_triples
from app.graph.scope import GraphScope, use_graph_scope
from app.graph.service import GraphService
from app.graph.store import SEED_TRIPLES, GraphStore

__all__ = [
    "Triple",
    "extract_triples",
    "GraphScope",
    "use_graph_scope",
    "GraphService",
    "GraphStore",
    "SEED_TRIPLES",
]
