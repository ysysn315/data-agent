# 评估体系实现说明（roadmap P1-2）

本项目有**两套评估**，共用一个理念：*不靠感觉说"Agent 挺准"，而是拿数据集量化它到底多准*。

- `evals/rag/` —— **检索/生成评估**（迁移后的数据集、指标、实验脚本与历史 baseline，见 §3）。
- `evals/text2sql/` —— **Text-to-SQL 执行准确率评估**（本次新增，项目差异化亮点）。

> 简历叙事对照（roadmap §5）："建立检索与生成双评估体系，SQL 执行准确率 X%"。
> 大多数简历项目做不到"能量化自己的 Agent 有多准"——这正是评估体系的价值。

---

## 一、两套评估是什么、怎么跑

### 1. RAG 评估（`evals/rag/`）

针对独立 RAG 实验链路（分块 → 混合检索 → 重排 → 生成）。注意：主 Chat 当前只接
Milvus 稠密召回与元数据后过滤，完整实验链路尚未接回主 Chat：

- **检索指标**：Hit@k、Recall@k、MRR、Precision@k、NDCG@k、MAP（`metrics.py`）。
- **生成指标**：关键词召回 keyword_recall、来源命中 source_hit，以及严格版
  事实点召回/幻觉惩罚/来源精确率（`metrics.py` 的 `*_strict`）。
- **数据集**：`datasets/rag_retrieval_cases.json`（40 例）、
  `rag_generation_cases_formal_template.json`（60 条分层模板）等。
- **基线**：`baselines/retrieval_baseline.json`、`generation_baseline.json`——
  改检索参数（top_k / hybrid / rerank）后与基线对比，看是涨还是跌。

运行需要 Milvus、Embedding、可选重排模型和测试语料。迁移后脚本仍引用已调整的配置字段，
部分语料目录也未随仓库提交；以下是入口而不是“开箱即得 baseline”的保证，执行前先按
`evals/rag/README.md` 的检查清单修正配置：

```bash
.venv/bin/python -m evals.rag.run_retrieval_eval      # 检索
.venv/bin/python -m evals.rag.run_generation_eval     # 生成
.venv/bin/python -m evals.rag.run_embedding_compare   # embedding 横向对比
```

### 2. Text-to-SQL 评估（`evals/text2sql/`）

针对"中文问题 → SQL → 结果"这条链路，量化**执行准确率（execution accuracy）**：

```bash
# 先备好演示库（合成模式，固定种子可复现）
python scripts/import_ecommerce.py --synthetic --db ./data/ecommerce.db
# 跑评估（--limit 抽样；--model 覆盖模型）
.venv/bin/python -m evals.text2sql.run_execution_eval --limit 10
```

流程（`run_execution_eval.py`）：**M-Schema + sql-generation 技能正文组装 prompt →
LLMFactory 调 LLM 生成 SQL → `sql_guard.validate_sql` 校验 → 执行 → 与 golden 结果集
按 execution accuracy 对比**，报告落 `reports/execution_latest.json`（总分 + 按 tags
分桶 + 每例明细）。它把项目三块能力（Skills 即提示词模板 / M-Schema / sqlglot 校验）
串起来做端到端度量。

- 数据集 `dataset.json`：28 例，难度分层 `tags` ∈ {单表聚合, 多表JOIN, 时间过滤, TopN, CTE}。
- 当前仓库有 3 份可区分的模型报告：qwen3.7-plus 25/28（89.29%）、
  qwen3-coder-plus 24/28（85.71%）、qwen3-coder-flash 23/28（82.14%）。
  `execution_latest.json` 当前与 coder-flash 相同，不代表最高成绩。
- 离线测试（`tests/test_text2sql_eval.py`）用原生 sqlite3 跑 golden，避免依赖 LLM 与网络。

---

## 二、实现原理

### execution accuracy 的定义与归一化对比（`common.py`）

**为什么比结果不比 SQL**：同一个问题的正确 SQL 有无数种写法（别名不同、JOIN 顺序
不同、子查询 vs. CTE、`SUM` 前后加不加 `ROUND`）。只要跑出来的**结果集**一致，就该判对。
所以判定口径是"把 golden SQL 与模型 SQL 分别在同一库上执行，比较两个结果集是否等价"。

"等价"要吸收三类无关差异（`compare_result_sets`）：

1. **列序无关**：`SELECT state, gmv` 与 `SELECT gmv, state` 等价。实现上把每行的
   单元格按稳定 key（类型档位 + 归一值）排序后再比 —— 交换列即交换行内单元格顺序。
2. **行序**：**仅当 golden 无 ORDER BY 时**忽略行序（`golden_has_order_by` 只看最外层，
   CTE/子查询内部的 ORDER BY 不算）。无序时按**多重集合**（Counter）比较，既忽略行序
   又保留重数（去重会放过"重复行数不同"的错）；golden 显式 ORDER BY 时逐行有序比较。
3. **浮点容差**：金额/均值列量化到固定小数位（默认 2 位）再比，吸收 `ROUND` 与原始
   浮点、以及浮点累加末位抖动；顺带让 `COUNT` 的 int 与等值 float 判等。

### 数据集分层设计（`dataset.json`）

28 例按 SQL 能力**难度分层**打 `tags`，一个用例可带多个标签（分桶各计一次）：

| 标签 | 覆盖能力 | 例 |
|---|---|---|
| 单表聚合 | GROUP BY / COUNT / SUM / AVG / WHERE | "每种订单状态各有多少订单" |
| 多表JOIN | 2~3 表 JOIN + 聚合 + 别名 | "各州客户贡献的销售额" |
| 时间过滤 | TEXT 时间列区间过滤 / strftime 按月 | "2017 年每月订单数" |
| TopN | ORDER BY + LIMIT（行序敏感） | "客户数最多的前 5 个州" |
| CTE | WITH 子句 / 派生表 | "各州销售额取前 5" |

分桶报告用于定位"哪类查询最弱"（如 CTE 明显低于单表聚合），指导后续 prompt 调优。
每条 golden 都先在合成库上真实执行验证过、且保证有结果（离线测试逐条守护）。

---

## 三、参考与差异化

- **my-agent-original 的 evals 体系（本项目的底座）**：检索侧的 Hit@k / Recall@k / MRR /
  NDCG / MAP 全部沿用其 `metrics.py`（原样迁回，未改），并保留 `baselines/` 做**基线对比**
  （调参后与基线比涨跌）。Text-to-SQL 评估是它"检索/生成评估"范式向 SQL 域的延伸：
  把"命中率"换成"执行准确率"，把"gold_sources"换成"golden 结果集"。
- **Yuxi 的 benchmark 生成思路**（`backend/package/yuxi/knowledge/eval/benchmark_generation.py`）：
  Yuxi 用 LLM 从知识库 chunk **自动生成**评测问答对（图 PPR 扩展选相关 chunk、可控并发），
  免人工标注、可规模化。本项目 demo 规模小，`dataset.json` 走**人工精标 + 真实执行校验**
  更可控可讲；但"用 LLM 批量造 question→SQL 对"是明确的扩展方向（与 SQL 示例库 P1-3 相通）。
- **SQLBot 没有评估体系**：SQLBot 提供了 M-Schema、方言提示词、行列权限等领域能力，
  却**没有**量化 Text-to-SQL 准确率的评估闭环。这正是本项目的差异化 ——
  我们抄了它的 M-Schema 和分层提示词，却补上了它缺的"能量化准确率"这一环。

---

## 四、取舍（面试可追问点）

1. **为什么用 execution accuracy 而不是 SQL 文本相似度**：文本相似度（编辑距离 /
   BLEU）会把语义等价的不同写法判错，也会把"长得像但跑出来不同"的判对 —— 与"答案对不对"
   几乎无关。执行准确率直接对齐业务目标：**用户要的是正确数据，不是正确字符串**。
2. **为什么 golden 结果集对比要做归一化**：不归一会把大量"其实答对"的判错（换了列序、
   聚合结果无序、金额末位差 0.001），指标虚低、失去指导意义。归一化把"无关差异"剔除，
   只留"真实差异"。代价是取了一个刻意的简化：列序无关靠"行内单元格排序"实现，极端下
   `(1,2)` 与 `(2,1)` 会判等 —— demo 的业务查询里几乎不出现同类型可互换列，可接受。
   行序敏感性绑定 golden 是否有 ORDER BY，也是一个约定：TopN 题的"排序"本身是考点。
3. **为什么离线测试不调 LLM**：LLM 输出不确定、依赖网络与密钥、有 token 成本，进不了
   CI，也无法稳定复现。所以 `tests/test_text2sql_eval.py` 只测**确定性**部分 ——
   golden 能跑通、归一化对比的正/反例、报告聚合；把"模型到底多准"留给 `run_execution_eval`
   这个需要真实 LLM 的线下脚本。测试守护"评估工具本身正确"，脚本产出"模型准确率数字"。
4. **为什么离线测试执行 golden 用原生 sqlite3 而非 validate_sql**：golden 是评测标准答案，
   只需要证明能在目标数据库执行并产生结果；把运行期策略校验耦合进 golden 守护，会让校验策略变化
   反过来污染评测基准。模型生成 SQL 的运行期链路仍走 `validate_sql`。
