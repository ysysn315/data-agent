# Data Agent 开发路线图 —— 融合 Yuxi / SQLBot 先进特性

> 定位：简历项目，时间有限。原则：**每一项都要能在面试里讲出"为什么这么设计"**，
> 宁可少而深，不做只能演示不能追问的功能。
> 参考项目功能全景对照见 REQUIREMENTS.md §10。

## 0. 现状（已完成 ✅）

| 能力 | 来源 | 状态 |
|---|---|---|
| LLM 抽象层（任意 OpenAI 兼容接口） | 自研（解决 my-agent 绑死 ChatTongyi） | ✅ |
| Agent 架构：langchain v1 create_agent + 中间件 | Yuxi | ✅ |
| Skills v2：目录模型/渐进披露/激活门控/远程安装 | Yuxi | ✅ |
| MCP：注册表/工具加载/技能联动 | Yuxi | ✅ |
| 工具熔断/重试/降级（ToolRuntimeMiddleware） | my-agent 保留 | ✅ |
| RAG 链路代码（分块/混合检索/BGE 重排） | my-agent 保留 | ✅（未接回 chat）|
| 只读 SQL 执行（execute_sql，引擎级只读） | SQLBot 思路 | ✅（demo 级）|
| pytest 测试体系（118 用例，随批次持续增长） | — | ✅ |
| Text-to-SQL 执行准确率评估 92.9%（28 例）+ 4 模型对比 | my-agent 思路扩展 | ✅ |
| Langfuse 调用链追踪（默认关闭） | Yuxi | ✅（PR #11）|

## 1. P0 —— demo 跑通闭环（必做，约 1 周）

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

## 2. P1 —— 差异化亮点（简历主叙事，约 2 周）

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
6. **schema embedding 检索（抄 SQLBot，1-2 天，表多时才有意义）** ⭐⭐
   表结构向量化 → 按问题召回 top-N 注入。9 张表的 demo 可以先讲"预留"，
   接口已在 schema-retrieval 技能占位。

## 3. P2+ 推进计划（4 轮子代理，轮间等待合并）

> 2026-07-26 更新：应项目主人要求，容器沙箱与知识图谱**排入计划**（此前"明确不做"
> 是措辞过重）。排序依据：依赖关系（持久化是地基、用户体系是权限前提）+ 冲突面控制。
> 难任务子代理可升级 fable-high 及以上模型。

| 轮次 | 分支 | 内容 | 难度 |
|---|---|---|---|
| D | feat/persistence | SQLAlchemy 2.0 async + SQLite（PG 就绪），skills/mcp/SQL示例/术语统一入库 | 大 |
| D | feat/async-tasks | ARQ + Redis 事件流，长任务提交与进度 SSE | 中 |
| D | feat/skill-embedding-match | skills 匹配升级 embedding 召回（复用 app/rag），可回退 jieba | 小 |
| E | feat/analysis-agent | P-O-R 工作流 + Markdown 分析报告，长任务走异步通道 | 中大 |
| E | feat/script-sandbox | 技能脚本执行升级容器沙箱（只读挂载/资源限制/超时，可切回 subprocess）；远程技能从此可安全启用 | 难⭐ |
| E | feat/knowledge-graph | LLM 三元组抽取 + 轻量图存储（SQLite 边表 + NetworkX，Neo4j 留接口）+ 图查询技能 | 难⭐ |
| F | feat/auth-workspace | 用户体系 + API Key 真鉴权 + 工作空间（多租户-lite） | 中大 |
| F | feat/frontend-v2 | 前端补页：分析报告 / 任务进度 / 图谱 / 示例与术语管理 | 中 |
| G | feat/row-col-permission | 行列级数据权限（JSONB 规则引擎，作用于 execute_sql 与数据源层），依赖 F | 中大 |

每项继续强制：四段式 IMPLEMENTATION.md、pytest 全绿、中文 commit、验收后 PR。

## 4. 暂缓项（非"不做"，提出即可排期）

> 旧版本此节叫"明确不做"，属 AI 拟稿措辞过重；容器沙箱与知识图谱已排入上方 E 轮。

- 12 种数据源方言（先用 SQLite + PostgreSQL 验证架构，接新方言只是配置量）
- 看板/大屏、嵌入式集成、i18n

## 5. 简历叙事对照

| 简历句 | 支撑点 |
|---|---|
| "基于 langchain v1 中间件架构实现 Skills 插件系统（渐进式披露/激活门控），上下文注入成本与技能数量解耦" | Skills v2 + 测试 |
| "实现 MCP 标准化工具接入，技能激活后懒加载外部工具" | MCP 系统 |
| "Text-to-SQL 全链路：M-Schema + 分层提示词 + sqlglot 校验 + 引擎级只读" | P0-2/3 + P1-1 |
| "建立检索与生成双评估体系，SQL 执行准确率 X%" | P1-2 |
| "工具调用熔断/降级机制，外部依赖故障时 Agent 降级续跑" | ToolRuntimeMiddleware |
