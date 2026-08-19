# 评测结果记录（50 题时代）

> 本页是横评与消融结论的持久化入口；报告 JSON 在 `text2sql/reports/` 与 `rag/reports/`。
> 28 题时代的历史报告（qwen3 系）保留在原处，仅作历史参照，不可与 50 题数字混比。

## Text-to-SQL 执行准确率（50 例，2026-08-16~18，aigc 网关）

| 模型 | 准确率 | easy/medium/hard | 报告 |
|---|---:|---|---|
| glm-5.2 | **94%（47/50）** | 100%/90%/95% | `execution_glm-5.2.json` |
| glm-5.3 | 92%（46/50） | 100%/90%/90% | `execution_glm-5.3.json` |
| deepseek-v4-flash | 90%（45/50） | 100%/80%/95% | `execution_deepseek-v4-flash.json` |
| MiniMax-M3 | 84%（42/50） | 100%/80%/80% | `execution_MiniMax-M3.json` |
| gemini-3.6-flash | 74%（37/50） | 100%/90%/45% | `execution_gemini-3.6-flash.json` |
| deepseek-v4-pro | 56%*（28/50） | 100%/70%/20% | `execution_deepseek-v4-pro.json` |
| LongCat-2.0 | 50%（25/50） | 50%/45%/55% | `execution_LongCat-2.0.json` |

**跳过与口径说明**（面试/回溯时以本页为准，不再翻 commit）：

- **kimi-k3 跳过**：aigc 网关对该 App 的 kimi-k3 RPM 配额≈0（单发即 429、间隔重试与退避均无效），非网络问题。
- **qwen 系跳过**：aigc 网关 400「不支持的模型类型」（qwen3.8-max/qwen3.7-plus 等均无）；DashScope 旧 key 已失效。
- **deepseek-v4-pro 的 `*`**：该模型经 langchain 层有随机空响应（同题约 2/3 概率 content 空、reasoning_content 未透传，17s 即返回非超时），56% 受此拖累，真实水平更高——**结论性引用建议标注"输出通道不稳定"**。
- **glm 两份报告为限流修复后重跑**：首版报告失败 27/37 例为 429 限流假失败（40%/22% 为污染数字）；评测脚本已加 `--interval` 与 429 指数退避，重跑后 0 污染。
- 环境备注：aigc.sankuai.com 需绕过本地代理（`no_proxy=sankuai.com`）；报告 `meta` 含完整环境与参数（interval/max_tokens/rate_limited_cases）。

## RAG 消融（2026-08-17~19，语料 12 篇 35 chunks，bge-m3 embedding）

### 检索侧（40 例，`ablation_retrieval_*.json`）

| 配置 | Hit@3 | MRR | NDCG@3 |
|---|---:|---:|---:|
| A 纯向量 | 72.5% | 0.621 | 0.625 |
| B +混合检索（BM25 RRF） | 80.0% | 0.658 | 0.677 |
| C 向量+LLM 重排 | 90.0% | 0.821 | — |
| **D 混合+LLM 重排** | **95.0%** | **0.842** | **0.864** |
| E 向量+BGE 重排 | 70.0% | 0.504 | — |
| F 混合+BGE 重排 | 72.5% | 0.483 | — |

**结论**：混合检索 +7.5pp、LLM 重排 +17.5pp、全开 +22.5pp（Hit@3 72.5%→95%）；**BGE 本地重排在本语料上反而低于基线**——碎块（40-140 字）判别力退化 + 领域陷阱（容量规划≠故障排查类 false-friend）需意图理解。重排选型要跟语料形态走。

### 生成侧（60 例模板，生成模型 glm-5.3 固定，`ablation_generation_*.json`）

| 检索输入 | 关键词召回 | 来源命中 | 禁引违规率 |
|---|---:|---:|---:|
| dense 检索 | 60.1% | 81.7% | 46.7% |
| **全开+LLM 重排** | **61.8%** | **95.0%** | **31.7%** |
| 全开+BGE 重排 | 55.3% | 81.7% | 48.3% |
| 无检索对照 | 32.5% | 6.7% | 0% |

**结论**：无 RAG 时关键词召回减半（模型不具备领域事实）；检索质量直接决定引用可信度（source_hit 81.7%→95%）；BGE 的检索劣化沿链路传导到生成。

### 已知局限

- 生成侧 no-RAG 对照组与 RAG 组的 prompt 不完全同构（无检索时无上下文可注入），keyword_recall 对比含 prompt 风格差异；结论以 source_hit/违规率为主。
- BGE 为 CPU 推理（bge-reranker-base）；LLM 重排调 glm-5.3——两者对比混合了"本地小模型 vs 大模型"与"交叉编码 vs 生成式排序"两个变量，引用结论时注明。
