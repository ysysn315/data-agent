# Data Agent 开发路线图 —— 融合 Yuxi / SQLBot 先进特性

> 定位：简历项目，时间有限。原则：**每一项都要能在面试里讲出"为什么这么设计"**，
> 宁可少而深，不做只能演示不能追问的功能。
> 参考项目功能全景对照见 REQUIREMENTS.md §10。
>
> 状态口径：本页顶部能力盘点按当前分支代码维护；P0/P1 与 D/E/F 轮次保留为开发历史。

## 0. 现状（已完成 ✅）

| 能力 | 来源 | 状态 |
|---|---|---|
| LLM 抽象层（任意 OpenAI 兼容接口） | 自研（解决 my-agent 绑死 ChatTongyi） | ✅ |
| Agent 架构：langchain v1 create_agent + 中间件 | Yuxi | ✅ |
| Skills v2：目录模型/渐进披露/激活门控/远程安装 | Yuxi | ✅ |
| MCP：注册表/工具加载/技能联动 | Yuxi | ✅ |
| 工具熔断/重试/降级（ToolRuntimeMiddleware） | my-agent 保留 | ✅ |
| RAG 知识库与实验链路 | my-agent 保留 | ✅ 主 Chat 共享 VectorStore，首次初始化恢复 BM25，统一改写/扩展、RRF、元数据过滤与可选重排；评测仍独立运行 |
| 数据源接入 + 自动 Schema 扫描 + AI 语义草稿/人工审核 | SQLBot 思路 + 自研闭环 | ✅ SQLite/PG/MySQL；真实远端 smoke 待环境 |
| Schema 相关表召回 | SQLBot | ⏳ `question` 已预留但未参与筛选；当前全量返回所选 Schema |
| 只读 SQL 执行（execute_sql，按方言 AST + 数据库只读） | SQLBot 思路 | ✅ |
| pytest 测试体系（23 个 test_*.py 文件，含 conftest 共 24 个 Python 文件） | — | ✅ pytest/CI 验证；Docker/Redis 不可用时外部集成用例按环境跳过 |
| Text-to-SQL 执行准确率评估（28 例）+ 模型对比 | my-agent 思路扩展 | ✅ 3 份可区分报告，最高 89.29%（25/28） |
| Langfuse 调用链追踪（默认关闭） | Yuxi | ✅ |
| 持久化层（应用状态、图谱、用户与数据源语义目录） | Yuxi | ✅（D 轮起，持续扩展）|
| 异步执行（ARQ + Redis Streams + SSE） | Yuxi | ✅（D 轮）|
| 技能语义匹配（embedding + jieba 回退） | 自研增量 | ✅（D 轮）|
| Analysis Agent（P-O-R 工作流 + Markdown 报告） | my-agent | ✅（E 轮）|
| 技能脚本容器沙箱（Docker 一次性容器） | Yuxi | ✅ 可切换；默认 subprocess，真机记录见实现文档 |
| 知识图谱基础（三元组抽取 + graph_search 技能） | Yuxi | ✅（E 轮）|
| 知识图谱平台化（workspace/datasource 作用域、实体消歧、路径工具、Schema 同步、可选 Embedding） | 自研 | 🚧 当前分支待 PR |
| 用户体系 + API Key 鉴权 + workspace-lite（默认关闭） | Yuxi/SQLBot | ✅（F 轮，非全资源租户隔离）|
| 前端 v2：任务中心 / 图谱 / 知识管理 / 数据源语义审核 | — | ✅ |

## 1. P0 —— demo 跑通闭环（已完成，保留历史计划）

**目标：一句中文问题 → 正确 SQL → 真实数据 → 回答。这是项目的"存在证明"。**

1. **导入 Kaggle 演示数据**（0.5 天）
   Brazilian E-Commerce CSV → SQLite（脚本入 `scripts/import_ecommerce.py`），
   REQUIREMENTS §5 已写明表结构。
2. **M-Schema 表结构描述**（抄 SQLBot，1 天）
   `schema_search` 工具：生成 SQLBot 风格的 M-Schema 文本
   （`# Table: orders, 订单表 [(order_id:INTEGER, 订单ID), ...]`），
   接进 schema-retrieval 技能（先全量注入，表少时不需要检索）。
3. **Text-to-SQL 提示词分层**（抄 SQLBot，1 天）
   sql-generation 技能的 SKILL.md 升级：主规则 + SQLite 方言规则 +
   零容忍规则（默认 LIMIT、禁止增删改、多表必须别名）——
   **技能即提示词模板，正好展示 Skills 系统的价值**。
4. **真实 LLM 端到端联调**（0.5 天）
   配好 .env（DashScope compatible-mode 或 FRIDAY），走通
   `POST /api/chat`："各州销售额 Top5" → read_skill → execute_sql → 回答。
5. **README + 启动脚本**（0.5 天）
   docker-compose 起 Redis（Milvus 可选），`uvicorn` 一条命令起后端。

## 2. P1 —— 差异化亮点（大部分已完成，保留历史计划）

**优先级按"面试可讲深度 ÷ 实现成本"排序。**

1. **SQL 校验与安全（抄 SQLBot，2 天）** ⭐⭐⭐⭐⭐
   sqlglot 解析：语法校验、表名/字段名存在性检查（对着 schema）、
   自动补 LIMIT、拦截 DDL/DML。讲点：Text-to-SQL 的可靠性工程。
2. **评估体系复活（my-agent 强项 + 扩展到 SQL，3 天）** ⭐⭐⭐⭐⭐
   - 迁回 evals/rag 的数据集与 baseline（my-agent-original 有现成的）
   - 新增 Text-to-SQL 评估：20-30 个 question→SQL→期望结果 用例，
     执行结果对比（execution accuracy），出准确率报告
   - 讲点：**"我能量化我的 Agent 有多准"，绝大多数简历项目做不到**
3. **SQL 示例库 / 数据训练（抄 SQLBot 核心卖点，2 天）** ⭐⭐⭐⭐
   question→SQL 对存储 + few-shot 注入 prompt + 反馈接口（答对存档）。
   讲点："越问越准"的运营闭环。
4. **术语库（抄 SQLBot，1 天）** ⭐⭐⭐
   业务术语映射（GMV=成交总额），命中即注入 prompt。实现小、故事完整。
5. **Chat 前端（复用 my-agent-original/frontend，2 天）** ⭐⭐⭐
   Vue3 对话界面 + skills 面板（列表/启停/详情）。demo 观感所需的最小集。
6. **schema embedding 检索（⏳ 未完成；抄 SQLBot，表多时才有意义）** ⭐⭐
   目标是表结构向量化 → 按问题召回 top-N，并补充外键相邻表后注入。当前演示库 6 张表、
   平台数据源为任意表规模；`question` 只在接口中预留，当前仍全量返回所选 Schema。

## 3. P2+ 推进计划（4 轮子代理，轮间等待合并）

> 2026-07-26 更新：应项目主人要求，容器沙箱与知识图谱**排入计划**（此前"明确不做"
> 是措辞过重）。排序依据：依赖关系（持久化是地基、用户体系是权限前提）+ 冲突面控制。
> 难任务子代理可升级 fable-high 及以上模型。

| 轮次 | 分支 | 内容 | 难度 |
|---|---|---|---|
| D ✅ | feat/persistence | SQLAlchemy 2.0 async + SQLite（PG 就绪），skills/mcp/SQL示例/术语统一入库 | 大 |
| D ✅ | feat/async-tasks | ARQ + Redis 事件流，长任务提交与进度 SSE | 中 |
| D ✅ | feat/skill-embedding-match | skills 匹配升级 embedding 召回（复用 app/rag），可回退 jieba | 小 |
| E ✅ | feat/analysis-agent | P-O-R 工作流 + Markdown 分析报告，长任务走异步通道 | 中大 |
| E ✅ | feat/script-sandbox | 技能脚本可切换一次性容器（只读挂载/资源限制/超时）；远程技能仍默认禁用并需人工审查 | 难⭐ |
| E ✅ | feat/knowledge-graph | LLM 三元组抽取 + 轻量图存储（SQLite 边表 + NetworkX，Neo4j 留接口）+ 图查询技能 | 难⭐ |
| F ✅ | feat/auth-workspace | 用户体系 + API Key 真鉴权 + 工作空间（多租户-lite） | 中大 |
| F ✅ | feat/frontend-v2 | 前端补页：分析报告 / 任务进度 / 图谱 / 示例与术语管理 | 中 |
| G | feat/row-col-permission | 行列级数据权限（JSONB 规则引擎，作用于 execute_sql 与数据源层），依赖 F | 中大 |

每项继续强制：四段式 IMPLEMENTATION.md、pytest 全绿、中文 commit、验收后 PR。

## 3.1 调研驱动的演进方向（2026-08-03 补充）

> 两份内部技术调研的提炼，已排除需线上流量 / 多集群 / 多租户结算等个人项目不具备的条件。
> 调研全文见 `docs/research/`（脱敏版，仅保留可复用的技术思路）。

### 架构与知识层（来源：某生产级数据 Agent 平台调研）

该平台是同赛道生产级前辈，**印证了 data-agent 现有关键决策**：单 Agent + SubAgent（非多 Agent）、
规范前置（非事后 lint）、Skill 内置固定路径优先于每次自主规划、工具熔断降级、TUI + GUI 双层呈现。
**调研的最大价值是「现有架构被生产级验证」这个叙事点，而非照抄功能**——下列方向按优先级排期，
低优先级项保留但待真实需求出现再动手：

| 方向 | 说明 | 优先级 |
|---|---|---|
| 评测基准层扩展 | 参考 Spider / BIRD / ELT-Bench 建基线，扩展现有 text2sql + RAG evals（已具备基础数据集与指标） | 🔴 高 |
| 知识飞轮（单人版） | 个人踩坑库 + 采纳反馈蒸馏置信度 + 长期未验证自动降权退场。该平台自认区别于通用 Agent 的核心差异化 | 🟡 中（最小版已落地：对话一键沉淀 / 评测失败导入候选 / 人工转正闸 / 作用域隔离，见 `docs/openspec/sql-knowledge-loop.md`；自动蒸馏与降权退场待做） |
| 上下文四层降级 | 历史裁剪 → 截断工具返回 → 模型摘要 → 紧急兜底，按代价从低到高逐层收敛。data-agent 目前只有摘要 | 🟢 低（demo 场景上下文很少爆，待长对话场景出现再做） |
| 规范分层 | 术语库从单层扩到全局 / 业务线两层 | 🟢 低（收益边际，可有可无） |

**四条方法论**（该平台踩坑总结，可作 data-agent 决策原则）：工具先行（核心工具成功率 80%+ 才上
知识增强）、能固定路径的不做自主规划、知识晋升需独立命中 + 审核双重门槛、虚拟专家靠蒸馏不靠配规则。

### 沙箱工程（来源：某生产级沙箱平台调研）

该平台是 data-agent sandbox 的生产级放大版，**核心隔离思路被印证**：一次性容器 + 断网 + 只读 +
资源限额 + 超时兜底回收 + 协议抽象 + 故障降级。三个轻量改进可落地（均对接现有机制，不引入重依赖），
按优先级排期：

| 改进 | 说明 | 对接点 | 优先级 |
|---|---|---|---|
| 资源规格预设档 | `lite` / `standard` / `heavy` 三档，技能 SKILL.md 声明所需档位（轻脚本 / 数据处理 / 重计算） | `skill_sandbox_image/memory/cpus` 配置 | 🔴 高（简单、自洽、能讲清） |
| 幂等 token 防重试重复执行 | `run()` 接收 `idempotency_key`，重试命中缓存直接返回上次结果，避免有副作用的脚本因重试执行两次 | `TOOL_POLICIES` 重试链路 | 🟡 中（链路绕、key/TTL/缓存存哪要设计；且当前技能脚本少有副作用，待真实场景出现再做） |
| 工作空间并发配额 | 工作空间维度 `max_concurrent_sandboxes`，超限返回降级文案，防本机 OOM | `auth.py` 工作空间 + `SandboxUnavailableError` | 🟢 低（analysis_agent 多为串行，并发耗尽本机概率低，待并行场景出现再做） |

MicroVM / 预热池 / Nydus 懒加载 / CRIU / 三级配额结算等大规模能力明确不适用，不排期。
详见 `docs/research/sandbox-platform-research.md`。

## 4. 暂缓项（非"不做"，提出即可排期）

> 旧版本此节叫"明确不做"，属 AI 拟稿措辞过重；容器沙箱与知识图谱已排入上方 E 轮。

- 12 种数据源方言（先用 SQLite + PostgreSQL 验证架构，接新方言只是配置量）
- 看板/大屏、嵌入式集成、i18n

## 5. 当前简历叙事对照

| 简历句 | 支撑点 |
|---|---|
| "Skills 渐进披露、激活门控与 MCP 按需加载；技能脚本可切 Docker 沙箱" | Skills/MCP/Sandbox 实现与测试；默认模式边界 |
| "M-Schema + 业务语义 + sqlglot AST + SQLite 只读" | Text-to-SQL、知识库、SQL Guard |
| "多格式 RAG + 混合检索实验链路 + 轻量知识图谱" | 主 Chat/实验链路分层；Graph 实现与测试 |
| "P-O-R 多步分析 + ARQ/Streams/SSE + 可选 Langfuse" | Analysis、Tasks、Tracing；历史回放而非断点续传 |
| "28 条 SQL 评测最高 89.29%；40+60 RAG 数据集" | 原始模型报告、RAG 数据集与指标 |
