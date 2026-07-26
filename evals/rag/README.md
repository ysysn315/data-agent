# RAG Evaluation

## What is evaluated

1. Retrieval:
- Hit@k
- Recall@k
- MRR

2. Generation:
- keyword_recall
- source_hit

3. Experiment loop:
- Compare top_k / enable_hybrid / enable_rerank

## Run

Use your conda env:

```bash
D:\Anaconda\envs\langchain-agent\python.exe evals/rag/run_retrieval_eval.py
D:\Anaconda\envs\langchain-agent\python.exe evals/rag/run_generation_eval.py
D:\Anaconda\envs\langchain-agent\python.exe evals/rag/run_experiments.py
```

## Knowledge Base Utility

清空当前知识库并重建当前 collection：

```bash
D:\Anaconda\envs\langchain-agent\python.exe -m evals.rag.clear_knowledge_base
```

批量导入 `aiops-docs` 和 `aiops-docs-noise` 文档：

```bash
D:\Anaconda\envs\langchain-agent\python.exe -m evals.rag.batch_index_test_docs
```
