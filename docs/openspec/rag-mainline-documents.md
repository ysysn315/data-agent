# RAG 主链路与文档索引 OpenSpec（已实现）

## 1. 目标与范围

本轮把 BM25+向量检索、查询改写/扩展、可选重排接入主 Chat，并提供基于 Milvus
持久化数据的文档列表接口。范围包括知识库单例、索引恢复、上传/删除后的派生索引更新、
请求级元数据过滤和来源事件；不包括分布式 BM25、GraphRAG 或线上自动评测闭环。

## 2. 数据与生命周期

Milvus 的 `content/metadata` 是持久化真相源，BM25 是进程内派生索引：

```text
get_vector_store()
  → _vector_store_lock 保证单例初始化
  → connect / ensure_collection
  → VectorStore（构造期注入 hybrid、rerank、上限和批大小）
  → restore_bm25_index()
  → 发布可用单例
```

`restore_bm25_index` 完成前不会把 VectorStore 返回给调用方，因此主 Chat、上传和文档
列表不会拿到“只有稠密检索、BM25 尚未恢复”的半初始化实例。单例初始化锁只保护创建
和恢复，不作为每次检索的全局锁；VectorStore 内部 `_bm25_lock` 只保护派生索引替换和
读取，不与单例锁嵌套。

## 3. 主 Chat 检索链路

```text
query_internal_docs
  → RAGService.retrieve_multi_query
  → QueryRewriter 改写/扩展
  → VectorStore.search
      ├─ Embedding + Milvus 稠密召回
      ├─ BM25 召回
      ├─ RRF 融合（document_key 去重）
      ├─ metadata 后过滤
      └─ 一次可选 rerank
  → record_sources
  → 返回上下文
```

`app/rag/document_utils.py` 统一文档身份约定：优先 `(source, chunk_index)`，缺少
`chunk_index` 时回退 `(source, content)`；来源提取也由同一模块负责。请求级过滤条件
和来源通过 `app/rag/context.py` 的 ContextVar 传递，请求退出时恢复 token，避免并发请求
之间串数据。

## 4. 同步 SDK 与事件循环边界

Milvus Python SDK 和 BM25 建索引/重建是同步操作，异步入口统一通过
`asyncio.to_thread` 执行：

- `search`：向量查询和 BM25 召回放入工作线程；Embedding 仍使用异步接口。
- `insert`：工作线程执行 `insert/flush`，随后更新 BM25 派生索引。
- `delete_by_source`：工作线程执行 `query/delete/flush`，随后重建 BM25。
- `restore_bm25_index`：工作线程完成 iterator 扫描和 BM25 建索引。
- `list_documents`：工作线程扫描并聚合文档摘要。

这不能消除 Milvus 网络耗时，但不会阻塞 FastAPI 事件循环；写入和删除仍通过同一个
VectorStore 单例执行。后续若支持多线程并发写入，需要再增加写锁或版本化索引交换。

## 5. 上限与文档列表语义

`RAG_BM25_MAX_DOCUMENTS` 同时限制恢复、增量 BM25 语料和文档列表扫描使用的 chunk 数，
均保留 iterator 扫描尾部，避免“恢复取前 N、增量取后 N”的策略分裂。文档列表是索引
视图：只列出本次保留 chunk 覆盖到的 source，并聚合 title、doc_type、chunk_count、
ingested_at 和 sheet_names；它不是独立的全量 document catalog。

当前保留“扫描尾部”，不保证严格按 `ingested_at` 的最近 N；Milvus iterator 顺序变化时，
列表和 BM25 子集可能变化。后续若需要大库全量、稳定排序和跨实例一致性，应新增持久化
document catalog/索引服务，而不是简单取消上限。

## 6. 验收与现状

- 主链路、BM25 恢复、文档接口、流式结构化事件和线程边界有回归测试。
- 本地开发环境基线：`PYTHONPATH=. .venv/bin/pytest -q -rs`；通过/跳过数量随 Docker、Redis
  和测试增量变化，提交时以 CI 实际结果为准。
- 跳过项是本机未启用的 Redis worker 与 Docker 沙箱；在不同运行环境中通过/跳过数量会变化。
- `ruff check .`、`ruff format --check .` 和前端 `npm run build` 作为提交前检查。
