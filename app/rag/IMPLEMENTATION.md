# RAG 主链路实现说明

> 本文记录主 Chat 已接入的 RAG 链路、单例边界、同步 SDK 的异步化处理和当前欠账。
> 评测脚本的独立实验链路见 [../../evals/rag/README.md](../../evals/rag/README.md)。

## 1. 组装与状态归属

`app/core/dependencies.py:get_vector_store` 是知识库唯一组装点：

```text
MilvusClient.connect/ensure_collection
  → EmbeddingService
  → VectorStore（hybrid/rerank/上限/批大小在构造期确定）
  → restore_bm25_index
```

`_vector_store_lock` 只保护单例初始化，避免多个请求各建一个 VectorStore；只有 BM25
恢复完成后才发布单例。VectorStore 内部另用 `_bm25_lock` 保护派生索引的替换，避免上传/删除工作线程与检索并发时读到半成品。BM25 是
进程内派生状态，Milvus 的 `content/metadata` 才是持久化真相源。RAG 配置在构造期
注入，不由 `get_chat_agent` 在单例创建后修改，避免“先访问上传接口、后访问 Chat”
造成行为漂移。

## 2. 主 Chat 检索链路

```text
Agent 调用 query_internal_docs
  → RAGService.retrieve_multi_query
  → QueryRewriter 改写/扩展
  → VectorStore.search（稠密召回 + BM25 + RRF）
  → 元数据过滤
  → 一次统一重排
  → 记录来源并返回上下文
```

`document_utils.py` 的 `document_key` 统一跨查询、稠密和 BM25 的去重键，
`document_source` 统一来源提取；`VectorStore._row_to_chunk` 统一 Milvus 行到 chunk
的转换。调用方不需要依赖 VectorStore 内部方法。
Milvus 的同步 `search` 放入 `asyncio.to_thread`，不会占用事件循环。

请求级过滤条件和来源通过 `ContextVar` 传递：路由进入 `use_metadata_filters`，工具
读取 `current_metadata_filters()`，检索命中后用 `record_sources()` 收集来源；请求退出
时 token 恢复，避免并发请求串数据。

## 3. 写入、恢复与文档列表

- 上传先按 `source` 删除旧 chunk，再由 `VectorIndexService` 解析、分块、向量化。
- `VectorStore.insert` 只在线程中执行同步 Milvus `insert/flush` 和 BM25 派生索引更新。
- `delete_by_source` 同样在线程中完成 Milvus `query/delete/flush` 与 BM25 重建。
- `search` 的同步 Milvus 查询和 BM25 召回也在线程中执行，事件循环只负责候选融合、过滤和契约编排。
- 首次创建单例时，`restore_bm25_index` 通过 `query_iterator` 分批读取 Milvus，使用
  `deque(maxlen=RAG_BM25_MAX_DOCUMENTS)` 保留扫描尾部，和增量写入的保留策略一致。
- `list_documents` 复用同一上限，聚合保留 chunk 覆盖到的 source、标题、类型、数量和
  Excel sheet；它是索引视图，不是独立的全量文档目录。超大库若要求不丢失旧 source，
  后续应增加持久化 document catalog，而不是取消上限把所有 chunk 读入内存。

## 4. 当前边界与验证

- BM25 是单进程派生索引，重启需从 Milvus 恢复；多实例部署需要共享索引或独立检索服务。
- “扫描尾部”依赖 Milvus iterator 返回顺序，不等价于严格按 `ingested_at` 的最近 N。
- BGE reranker 是可选依赖；未安装或未注入时回退配置的 LLM/融合排序。
- 主 Chat 与评测共用检索组件，但评测生成链路仍独立，不代表线上自动回归。
- 本机验证：`PYTHONPATH=. .venv/bin/pytest -q -rs`；Docker/Redis 集成用例是否跳过取决于运行环境。
