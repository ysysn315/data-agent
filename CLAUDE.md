# CLAUDE.md

本文件给在此仓库中工作的代码 Agent 使用。与用户交流、代码注释、提交信息和设计文档统一使用中文。

## 仓库定位

`data-agent` 是当前活跃项目。相邻的 `my-agent-original/`、`yuxi-reference/`、`sqlbot-reference/` 只用于设计对照；除非用户明确要求，不要修改参考仓库，也不要把参考实现的行为当成本仓库事实。

当前事实入口：

- 对外能力与已知边界：`README.md`
- 当前/后续状态：`docs/openspec/roadmap.md`
- 模块实现：各目录下 `IMPLEMENTATION*.md`
- 历史需求：`REQUIREMENTS.md`，仅作基线，不代表当前状态

## 常用命令

```bash
cd /Users/ysn/projects/data-agent

# 依赖
uv sync

# 演示数据
.venv/bin/python scripts/import_ecommerce.py --synthetic --db ./data/ecommerce.db

# 测试
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_skills_middleware.py -q

# 后端
.venv/bin/uvicorn app.main:app --reload --port 9900

# 前端
cd frontend && npm install && npm run dev

# 异步任务（需 Redis）
docker compose up -d redis
.venv/bin/arq app.tasks.worker.WorkerSettings

# Text-to-SQL 评测
.venv/bin/python -m evals.text2sql.run_execution_eval --difficulty hard --limit 10
```

默认 Chat/Text-to-SQL 不依赖 Milvus、Redis、Docker 或 Langfuse。知识库工具需 `ENABLE_KB_TOOL=true` + Milvus；Docker 技能沙箱需 `SKILL_SANDBOX_MODE=docker`；Langfuse 需开关和两把 key 同时就绪。

RAG 评测依赖外部语料、Milvus、Embedding 和可选重排模型。运行前先读 `evals/rag/README.md`，不要把仓库中的历史 baseline 当作当前可复现实验结果。

## 当前事实基线（2026-08-16）

- pytest/CI 结果以实际运行环境为准；Docker/Redis 不可用时，外部集成用例会按环境跳过。
- 5 个内置技能：schema-retrieval、sql-generation、sqlite-query、data-visualization、knowledge-graph。
- Text-to-SQL 当前数据集为 50 条（easy 10 / medium 20 / hard 20）；3 份模型报告均是扩容前 28 题历史报告，最高 `89.29%（25/28）`，不得冒充 50 题成绩。
- 主 Chat 的知识库工具已接入 Milvus 稠密召回、BM25 恢复、查询改写/扩展、RRF、元数据过滤和可选重排；BM25 是受上限约束的进程内派生索引。
- 技能脚本默认 `subprocess`；Docker 是可切换的一次性容器执行器。
- Redis Streams 能保存并回放历史事件，但 HTTP SSE 未暴露 `Last-Event-ID/after_seq`，前端不自动重连。
- Langfuse 默认关闭且只覆盖部分 Agent 图入口，不得描述成全链路可观测闭环。

## 关键架构接缝

### LLM 与 Embedding

所有聊天模型通过 `app/core/llm.py` 创建，避免在业务模块直接引入厂商专属 chat 类。Embedding 统一走 `app/rag/embeddings.py`。推理模型的 `reasoning_content` 与最终答案分流，避免思考文本污染历史消息。

### Skills / MCP

一个 Skill 是 `SKILL.md + 可选 scripts/` 的目录。运行时流程：

1. system prompt 只披露名称和描述；
2. 模型调用 `read_skill(slug)` 读取正文并激活；
3. 下一轮解锁该技能声明的本地工具，或懒加载对应 MCP server 工具。

LangChain v1 的两个约束：

- 本地门控工具必须在构建期进入 `AgentMiddleware.tools`，请求期只控制模型可见性；
- 动态 MCP 工具构建期不存在，执行时必须通过 `request.override(tool=实例)` 接管。

新增技能时同步检查 `dependencies.tools/mcps/skills`、依赖展开和门控测试。

### 工具运行时与脚本沙箱

`ToolRuntimeMiddleware` 统一处理工具超时、重试、熔断和错误回喂。新增外部工具时优先补 `TOOL_POLICIES`，不要在每个工具里重复实现重试。

`run_skill_script` 先校验技能已启用、脚本位于该技能 `scripts/` 下，再交给配置选择的 runner：

- `subprocess`：默认，只有路径、超时与输出长度限制，没有进程隔离；
- `docker`：一次性容器，断网、只读根文件系统/挂载、CPU/内存/PID 限额和超时回收。

远程技能仍默认禁用，必须人工审查后启用。Docker 模式也不是生产级多租户代码沙箱。

### Text-to-SQL

主链路是 M-Schema → 术语/SQL 示例 → LLM 生成 → sqlglot AST 校验 → SQLite `mode=ro` 执行。校验失败应以可供模型自纠的工具结果返回，不要用异常直接中断 Agent。

列校验采取“宁漏报、不误报”：多表、CTE、子查询无法可靠确定列归属时保守跳过，由数据库执行错误和只读模式兜底。

### RAG 与图谱

文档上传负责解析、表格语义化、分块、Embedding 和 Milvus 写入。主 Chat 使用 `VectorStore` 的稠密检索；不要在未修改接线前声称主链路已使用 BM25、查询改写或 BGE 重排。

图谱是 SQLite + NetworkX 的轻量实现，LLM 抽取需显式调用接口；它不是 Neo4j/GraphRAG，也没有自动接入文档上传。

### 持久化、鉴权与异步任务

- 应用配置数据通过 SQLAlchemy 2.0 async Repository 持久化；技能正文仍在文件系统，数据库保存索引/元数据。
- `AUTH_ENABLED=false` 保持 demo 模式；开启后才启用 API Key 守卫。workspace-lite 不是全资源多租户隔离。
- ARQ 负责后台执行，Redis Hash 保存任务状态，Redis Streams 保存阶段事件，SSE 负责传输。任务运行态带 TTL，不进入业务关系库。

## 开发约定

- 改动前先看目标目录最近的 `IMPLEMENTATION*.md`；若文档与代码冲突，以代码和测试为准，并在同一改动中修正文档。
- 非平凡子系统先更新 OpenSpec，再实现代码；历史方案文档保留原始决策，但必须在顶部标明是否仍代表当前状态。
- 依赖注入统一走 `app/core/dependencies.py`，不要在路由里声明同名依赖。
- 配置进入 `Settings` 并同步 `.env.example`；日志使用 loguru。
- 工作区可能有用户未提交改动。修改前先看 `git status`/`git diff`，不得覆盖无关内容。
- 每修一个缺陷补一条能在修复前失败的回归测试。

## 文档维护规则

- README 只保留当前能力、运行方式、可信数字和已知边界，不记录逐轮开发史。
- `docs/interview-guide.md` 的数字必须能回到测试或原始报告。
- `REQUIREMENTS.md` 与旧差距分析是历史基线；不要把其中“未实现/二期”直接复制到当前文档。
- 不使用会快速腐化的 PR 数量、分支名和“当前第 N 个测试”作核心叙事；测试总数只在有本轮实跑证据时更新。
