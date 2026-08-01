# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

（与用户交流、写代码注释、commit、设计文档，一律用中文。）

## 工作区布局

`/Users/ysn/projects` 不是一个仓库，而是装着四个独立 git 仓库的工作区：

| 目录 | 角色 | 对 data-agent 的贡献 |
|---|---|---|
| `data-agent/` | **当前活跃项目**，所有新工作都在这里 | — |
| `my-agent-original/` | 只读。data-agent 的前身 | 底座：LangGraph Agent、整条 RAG 链路、evals、FastAPI 分层 |
| `yuxi-reference/` | 只读。上游参考（语析 Yuxi） | 平台层：Skills、MCP、中间件、异步执行、多租户 |
| `sqlbot-reference/` | 只读。上游参考（DataEase SQLBot） | 数据域：Text-to-SQL、M-Schema、方言提示词、行列权限 |

三个参考仓库当**文档**用，不要改、不要提交。加功能前先看哪个参考项目已解决过，
读完它的实现再动手 —— 这是一个刻意的融合项目：**my-agent 是身体，Yuxi 是架构，SQLBot 是领域**。
`data-agent/REQUIREMENTS.md` §0 记录了每项能力的出处规划。

`yuxi-reference/` 自带 CLAUDE.md/AGENTS.md，只在改那个目录时生效（基本不该发生）。

## data-agent

中文项目「智能数据分析 Agent 平台」：FastAPI + langchain v1 create_agent，
Text-to-SQL + RAG，Skills 插件化 + MCP 标准化工具接入。
当前分支 `feat/skills-v2-mcp`：应用可启动、22 个 pytest 用例全绿。

### 常用命令

```bash
cd data-agent
cp .env.example .env        # LLM_API_KEY 必填（创建 LLM 时显式校验）

# 依赖（uv + pyproject.toml，Python 3.11）
uv pip install -p .venv -r pyproject.toml --group dev

# 测试
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest tests/test_skills_middleware.py -q   # 单文件

# 启动（chat 默认不依赖 Milvus；需要知识库检索时 ENABLE_KB_TOOL=true 并起 Milvus）
.venv/bin/uvicorn app.main:app --reload --port 9900

# RAG 评估（数据集需从 my-agent-original 迁回，见 roadmap P1-2）
.venv/bin/python -m evals.rag.run_retrieval_eval
```

基础设施按需：Redis（会话）:6379、Milvus（知识库，可关）:19530。
`my-agent-original/docker-compose.yml` 可整套拉起。
本地 BGE 重排/向量是重依赖（torch），已惰性导入，需要时另装：`uv pip install torch FlagEmbedding`。

### 架构：四个关键接缝

**`app/core/llm.py` — LLMFactory。** 所有 LLM 调用必须走这里（ChatOpenAI + 自定义
base_url，任意 OpenAI 兼容端点）。这是为了解除 my-agent 绑死 ChatTongyi 的教训，
**不要再引入任何厂商专属 chat 类**。embedding 同理（`app/rag/embeddings.py`）。

**`app/skills/` — Skills v2（目录模型 + 三段式）。** 一个 skill 是一个目录
（SKILL.md + 可选 scripts/）。运行时：披露（system prompt 只注入名称+描述）→
激活（模型调 `read_skill(slug)`，middleware 拦截结果写 state.activated_skills）→
解锁（该技能声明的门控工具变为可见；声明的 MCP 工具懒加载）。
langchain v1 的两个硬约束：门控本地工具必须挂 `AgentMiddleware.tools` 构建期注册；
动态 MCP 工具必须在 `wrap_tool_call` 里 `request.override(tool=实例)` 接管执行。
设计文档：`docs/openspec/skills-system.md`。

**`app/mcp/` — MCP 注册表与工具加载。** JSON 文件注册表（save_dir/mcp_servers.json），
MultiServerMCPClient 拉工具，带超时/失败隔离/配置哈希缓存。
注意：stdio transport = 服务器命令执行面，当前无真实鉴权，API 不能对外。
设计文档：`docs/openspec/mcp-system.md`。

**`app/agents/tool_runtime.py` + `app/agents/middlewares.py` — 工具熔断。** 每个工具
调用经 ToolRuntimeMiddleware 走重试/超时/熔断/降级（TOOL_POLICIES 按工具名配置），
失败返回降级文案而不是抛异常。熔断状态是进程级全局的，测试间要
`reset_tool_runtime_state()`。新工具加 TOOL_POLICIES 条目，不要自造错误处理。

其余分层沿袭 my-agent：`api/ → services/ → agents|rag|skills|mcp|tasks → clients/`，
路由统一挂 `/api`；单例一律从 `app/core/dependencies.py` 取
（不要在路由文件里定义同名依赖 —— 曾因遮蔽 get_skill_service 导致 API 恒返回空；
注意 asyncio.Lock 不可重入 —— get_chat_agent 曾因锁内套锁死锁）。

F 轮新增：**`app/core/auth.py`**（API Key 鉴权 + 工作空间，`AUTH_ENABLED` 默认 False
逐字节保持 demo 行为；开启后 bootstrap 自动签发 admin Key 打日志一次；保护清单单一事实源
`auth.PROTECTED_ENDPOINTS`，MCP 写口限 admin）。前端已含任务中心/图谱可视化/知识管理页。

E 轮新增：**`app/graph/`**（三元组抽取 + SQLite/NetworkX 图存储，graph_search 门控工具）、
**`app/agents/analysis_agent.py`**（P-O-R 工作流，每步复用 ChatAgent 完整技能三段式）、
**`app/skills/sandbox.py`**（脚本执行可切 Docker 一次性容器，`SKILL_SANDBOX_MODE=docker`；
本机 colima 需挂载 /private/var/folders，见 IMPLEMENTATION-sandbox.md 附录）。

D 轮新增两个基础模块：**`app/db/`** —— SQLAlchemy 2.0 async 持久化（SQLite 起步，
`database_url` 一行切 PG；skills 表只存元数据与 dir_path，SKILL.md 正文永不入库，
对齐 Yuxi"内容存 FS、索引存 DB"）；**`app/tasks/`** —— ARQ 异步任务 + Redis Streams
事件流 + SSE（worker 启动：`arq app.tasks.worker.WorkerSettings`；任务状态在 Redis
不入库）。技能匹配已升级 embedding 召回 + jieba 回退（`app/skills/matching.py`）。

### 约定

- **全中文**：注释、docstring、SKILL.md、设计文档、commit（Conventional Commits，
  中文主题 + 中文要点列表；把刻意的简化/欠账写进 commit body，这是项目的记账习惯）。
- **先 spec 后码**：非平凡子系统先在 `docs/openspec/` 写设计（定位→设计→文件结构→
  API→与参考项目差异）。
- 配置进 `Settings`（pydantic-settings）+ 同步 `.env.example`；日志用 loguru。
- 安全既定决策：脚本执行是无隔离 subprocess，所以**远程安装的 skill 默认
  enabled=False**；execute_sql 引擎级只读。放松任何一条前先看
  `docs/openspec/skills-system.md` §5。

### 路线图

`docs/openspec/roadmap.md`：P0（demo 闭环）与 P1（sqlglot 校验、评估体系、SQL 示例库、
术语库、前端）已完成；§3 是 P2+ 的 D/E/F/G 四轮子代理推进计划（持久化 → 分析Agent/
容器沙箱/知识图谱 → 用户体系/前端 v2 → 行列权限），难任务可给子代理升级 fable-high。
注意：**没有"明确不做"清单** —— 旧版那节措辞过重已更正为"暂缓项"，用户提出即可排期。
协作节奏：每轮分支 → PR → 等用户合并；每个功能配四段式 IMPLEMENTATION.md。
已验证成果：Text-to-SQL 执行准确率 92.9%（28 例、4 模型对比在 evals/text2sql/reports/）。
