# RAG 评测说明

本目录包含 RAG 数据集、指标、历史 baseline 和实验脚本。它用于独立切换模型/参数做评测；主 Chat 现在也共用同一套检索组件，但本目录的生成评测仍不是线上请求的自动回归。

## 当前能力边界

| 路径 | 当前能力 |
|---|---|
| 主 Chat | 知识库单例首次初始化恢复 BM25 → 查询改写/扩展 → Milvus + BM25 → RRF → Python 元数据后过滤 → 可选重排 |
| 独立实验链路 | 查询改写/扩展 → Milvus + BM25 → RRF → BGE/LLM 重排 → 去重 → RAG 生成 |

主 Chat 与上传/文档列表共用一个 VectorStore；知识库单例首次初始化时从 Milvus 分批恢复 BM25。主 Chat 默认使用配置的 LLM 作为重排兜底，本地 BGE 仍需显式安装并在代码/配置中注入。评测脚本也统一通过 `LLMFactory` 使用当前 OpenAI 兼容配置，不再依赖已移除的 `ChatTongyi`/旧配置字段。

## 数据集

- `rag_retrieval_cases.json`：40 条检索用例，覆盖普通、困难、噪声/易混淆查询。
- `rag_generation_cases_formal_template.json`：60 条分层生成模板，覆盖 single-hop、multi-hop、confusable 和 no-answer。
- 目录内其他 generation JSON 是迁移与构建过程中的中间版本，新增实验应明确记录实际使用哪一份。

## 指标

检索侧：Hit@K、Recall@K、MRR、Precision@K、NDCG@K、MAP。

生成侧：关键词召回、来源命中，以及严格版事实召回、来源精确率、幻觉和禁用来源惩罚。

## 历史 baseline

`baselines/retrieval_baseline.json` 包含 40 条旧链路结果；摘要为 Hit@3=0.875、MRR=0.7458、NDCG@3=0.7364。

`baselines/generation_baseline.json` 只有 8 条旧生成结果；摘要为 keyword_recall=0.975、source_hit=0.9375。

这些文件缺少与当前代码、模型和语料完整对应的实验产物，只能作为历史参照，不能据此宣称当前项目“相比 baseline 提升了 X”。

## 运行入口

```bash
.venv/bin/python -m evals.rag.run_retrieval_eval
.venv/bin/python -m evals.rag.run_generation_eval
.venv/bin/python -m evals.rag.run_embedding_compare
```

运行前必须确认：

1. Milvus 已启动，collection 维度与当前 Embedding 模型一致；
2. 当前 `.env` 已配置 Embedding/LLM 所需的 key、base URL 和 model；
3. 脚本通过 `LLMFactory` 使用 `app/core/settings.py` 当前配置字段；
4. 实验所需的正式文档与噪声文档目录存在；
5. 使用 BGE 时已按需安装 torch/FlagEmbedding 并准备模型缓存；
6. 输出单独落到带模型、数据集和配置标识的报告，不覆盖历史 baseline。

第 4 项依赖仓库外的正式文档目录；若缺少语料，本目录仍不是开箱即跑的可复现实验包。可先离线检查数据集和指标单元逻辑，但不要把缺少外部依赖归因于 RAG 算法本身。

## 知识库辅助脚本

- `clear_knowledge_base`：清空并重建当前 collection；属于破坏性操作，执行前确认目标 Milvus 环境。
- `batch_index_test_docs`：批量导入测试语料；依赖本地语料目录。

```bash
.venv/bin/python -m evals.rag.clear_knowledge_base
.venv/bin/python -m evals.rag.batch_index_test_docs
```
