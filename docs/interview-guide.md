# Data Agent 面试防御手册

> 用途：把这个项目讲清楚、并且**扛得住追问**。每条要点尽量标注了素材出处，便于回读原文。
> 三层结构约定：**① 一句话（30 秒）** 用于自我介绍；**② 展开（3 分钟）** 用于主动讲解；
> **③ 追问层** 是面试官会往下挖的高频问题 + 要点式答案（含大量"为什么不用 X"）。
> 配套：交互式模拟面试见 `.claude/skills/mock-interview/SKILL.md`（在 Claude Code 里输入 `/mock-interview`）。

目录：
- [§1 项目总述（三层版）](#1-项目总述三层版)
- [§2 六个子系统](#2-六个子系统各三层)
  - [2.1 Skills 三段式](#21-skills-三段式渐进披露--激活门控--工具解锁)
  - [2.2 Text-to-SQL 链路](#22-text-to-sql-链路)
  - [2.3 Agent 核心 + 工具熔断](#23-agent-核心--工具熔断)
  - [2.4 MCP 系统](#24-mcp-系统)
  - [2.5 评估体系](#25-评估体系)
  - [2.6 工程实践（并行开发 · 测试）](#26-工程实践并行开发--测试)
- [§3 八个踩坑故事（完整版）](#3-八个踩坑故事完整版)
- [§4 数字速记卡](#4-数字速记卡)
- [§5 简历句 → 证据映射](#5-简历句--证据映射)

---

## §1 项目总述（三层版）

### ① 一句话（30 秒）

> 我做了一个智能数据分析 Agent 平台：用户用中文提问，Agent 检索表结构、生成并校验 SQL、在真实电商数据上执行、返回表格回答。技术上融合了三个开源项目——Agent 平台与 Skills/MCP 机制学 Yuxi，Text-to-SQL 与知识闭环学 SQLBot，RAG 链路和评估体系保留自我自己的 my-agent。亮点是三个：Skills 的**渐进式披露**让上下文成本与技能数量解耦、Text-to-SQL 的**全链路可靠性工程**（M-Schema + 分层提示词 + sqlglot 校验）、以及**可量化的评估体系**（SQL 执行准确率 92.86%）。

### ② 展开（3 分钟）

- **定位与取舍**：这是简历项目，时间有限，原则写在 `docs/openspec/roadmap.md` 开头——"每一项都要能在面试里讲出为什么这么设计，宁可少而深，不做只能演示不能追问的功能"。所以砍掉了容器沙箱、知识图谱、多租户、12 种方言等重功能（`REQUIREMENTS.md` §9/§10 记账），聚焦能讲透的核心。
- **三条主叙事**：
  1. **Skills 系统**（学 Yuxi）：能力 = 一段提示词模板 + 可选门控工具；渐进披露 + 激活门控让"技能再多也不撑爆 system prompt"。
  2. **Text-to-SQL**（学 SQLBot）：把"自然语言 → SQL → 执行"做成有防护的链路，而不是让 LLM 裸生成裸执行。
  3. **评估体系**（保留 my-agent 强项并扩展）：用数据集量化 Agent 到底多准，这是绝大多数简历项目缺的一环。
- **工程侧**：langchain v1 `create_agent` + 中间件栈替换手写 StateGraph；`LLMFactory` 解除厂商绑定；10 个 PR 的开发史、113 个 pytest 用例、中文 Conventional Commits 全程可追溯。

### ③ 追问层

- **"为什么不直接用现成的 Text-to-SQL 产品（如 SQLBot 本身）？"**
  - SQLBot 是产品，我要的是**把它的设计拆开、理解、并补上它缺的一环**。SQLBot 有 M-Schema、分层提示词、行列权限，但**没有评估体系**（`evals/IMPLEMENTATION.md` §三）——我抄了它的能力，补上了"能量化准确率"。这个"理解 + 差异化"才是简历项目的价值，不是把产品跑起来。
- **"三个参考项目，你到底写了什么、抄了什么？"**
  - 每个模块的 `IMPLEMENTATION*.md` 都有"③ 参考项目怎么做的 / ④ 区别与取舍"。抄的是**设计思路**（三段式、M-Schema 格式、断路器），落地是自己写的，且都做了针对性修正（如 MCP 修了 Yuxi 的 4 个问题，见 §2.4）。my-agent 是我自己的项目，`tool_runtime.py` 是原样保留。
- **"如果让你重做，最想改什么？"**
  - 引入真正的持久化层（现在 Skills/MCP/示例库都是内存 + JSON 落盘，重启丢），这是二期第一优先级（`roadmap.md` §3）；以及把 schema 检索从"全量注入"升级为 embedding 召回（表数上量后才有意义，接口已在 `schema_search` docstring 预留）。
- **"这个项目最难的部分是什么？"**
  - 不是任何单个功能，而是**对齐 langchain v1 的真实中间件契约**：门控工具的可见性（请求期过滤）与可执行性（构建期注册）必须分开处理，动态 MCP 工具还得在 `wrap_tool_call` 里 override——踩了好几个"看起来完成了但实际没通"的坑（见 §3-③）。

---

## §2 六个子系统（各三层）

### 2.1 Skills 三段式（渐进披露 → 激活门控 → 工具解锁）

**① 一句话**：一个技能是一个目录（`SKILL.md` + 可选 `scripts/`）；system prompt 只注入名称+描述，模型 `read_skill(slug)` 按需读全文并激活，激活后其声明的门控工具才对模型可见——注入成本与技能数量、正文长度解耦。

**② 展开**：三段式（`docs/openspec/skills-system.md` §2）——
1. **披露**（`SkillsMiddleware.awrap_model_call`）：system prompt 追加每个技能的 `- **名称**: 描述` + "先调用 `read_skill(slug)`"。实测 4 个内置技能约 150 tokens/请求，与正文长度无关。
2. **激活**：模型调 `read_skill(slug)` → 工具返回 SKILL.md 全文；middleware 在 `awrap_tool_call` 拦截结果，把 slug 写进 LangGraph state 的 `activated_skills`（走 `Command(update=...)`）。
3. **解锁**：下一次模型调用时，已激活技能声明的 `dependencies.tools` 从"隐藏名单"移除、`dependencies.mcps` 的 MCP 工具懒加载追加。
- 依赖三类：`tools`（本地门控工具）/`mcps`（MCP server）/`skills`（其他技能，递归展开）。展开只沿 `skills` 边递归。

**③ 追问层**
- **"渐进式披露到底省了多少？给个数。"**
  - 全文注入：3 个内置技能约 2366 chars ≈ **1183 tokens/请求**，且无法关闭，外推 20 个技能 ≈ 7900 tokens（`skills-optimization.md` §2.2）。改名称+描述后 ≈ **150 tokens/请求**，且与正文长度无关。这是核心量化卖点。
- **"为什么不用语义匹配（embedding）自动挂载技能，而要模型自己 read_skill？"**
  - Yuxi 的理念就是"渐进披露让模型自己决定读哪个"，比预先语义匹配更贴合模型意图（`skills-optimization.md` §2.7）。项目里确实有 `match_skills_by_query`（jieba 关键词，是相对 Yuxi 的增量），但它是**可选增强**不是主路径；升级为 embedding 召回是二期（`app/rag` 已有向量化能力）。
- **"门控工具为什么要在构建期就注册？请求期过滤不就行了？"**
  - langchain v1 的 ToolNode 在**构建期**确定可执行工具集。若门控工具没进 `middleware.tools`，即便模型发起调用，执行器也判 "not a valid tool"（Yuxi `skills.py:151-164` 白纸黑字写了这个坑）。所以**可执行性=构建期注册，可见性=请求期从 `request.tools` 过滤**，两者分开。这是踩坑 §3-③。
- **"技能能带脚本，安全怎么保证？"**
  - `run_skill_script` 是**无隔离 subprocess**（`REQUIREMENTS.md` §9 决定不做重沙箱），防护三层：路径必须落在 `<skill>/scripts/` 内（`relative_to` 拦路径穿越）+ 30s 超时 + 8000 字输出截断。正因无隔离，**远程安装的技能默认 `enabled=False`**，人工审查后才经 API 启用（`app/skills/remote_install.py`）。
- **"为什么用 read_skill 专用工具而不是像 Yuxi 那样 read_file？"**
  - Yuxi 有每 thread 的容器沙箱文件系统，技能以 `/home/gem/skills/<slug>/SKILL.md` 存在，读文件即读技能。本项目没有沙箱，一个只读 `SkillService` 的专用工具就是**最小且最稳的边界**——不用解析路径、天然只能读技能目录（`app/skills/IMPLEMENTATION.md` ④）。
- **"为什么技能是一个目录，而不是一个字符串（SKILL.md 全文）？"**
  - v1 就是把整个 SKILL.md 存成 `Skill.content` 字符串，导致两个硬伤：①随附脚本被丢弃，技能无法携带可执行代码；②注入只能给全文，没法渐进披露（`skills-optimization.md` §1）。改成目录模型（`SKILL.md` + `scripts/`）后，`sqlite-query` 才能带 `scripts/query.py`、对标 Yuxi 的 `mysql-reporter`——对数据分析 Agent 来说，"技能能带着 SQL 脚本/图表模板一起分发"正是这个模式最大的价值。导入用 `copytree` → 临时目录 → 原子 `rename`、失败 `rmtree` 回滚，保证文件系统与存储层一致。

---

### 2.2 Text-to-SQL 链路

**① 一句话**：自然语言 → SQL 不是让 LLM 裸生成裸执行，而是 M-Schema（带中文注释的表结构）+ 分层提示词（写在 SKILL.md 里）+ sqlglot AST 校验 + 引擎级只读，四段防护，且用执行准确率量化。

**② 展开**：
- **M-Schema**（`app/text2sql/m_schema.py`）：SQLBot 风格 `# Table: orders, 订单表` + `[(order_id:TEXT, 订单ID), ...]`。SQLite 没有 `COMMENT ON`，中文注释外置到 `comments_ecommerce.py` 字典，**未命中即留空、绝不编造**（由 `test_generate_m_schema_no_fabricated_comment` 守护）。
- **分层提示词**（`sql-generation/SKILL.md`）：①主规则（先检索再写、逐字复核）②SQLite 方言规则（引号/时间/LIMIT）③零容忍规则（默认 LIMIT 1000、多表必须别名、只读、不编造）。写进 SKILL.md 而非硬编码，享受渐进披露、依赖展开、可覆盖。
- **sqlglot 校验**（`app/agents/tools/sql_guard.py`）：AST 解析 → 单语句约束 + 只读（判最外层节点类型，不是首词）+ CTE 排除 + 表列存在性 + 自动补 LIMIT，返回中文报错供模型自纠。
- **知识闭环**（`app/text2sql/examples.py` / `terminology.py`）：SQL 示例库（question→SQL few-shot）+ 术语库（GMV/复购率/客单价 统一口径），合并成 `sql_context_search` 一个工具，答对的问答可回流入库——"越问越准"。

**③ 追问层**
- **"为什么在 SKILL.md 里写提示词，不写在代码里？"**
  - 这是本项目相对 my-agent 的关键差异：Skills 让"能力=提示词模板+门控工具"。规则写进 `sql-generation/SKILL.md` 就享受渐进披露（不激活不占 prompt）、依赖展开（自动带出 schema-retrieval + schema_search）、可被用户覆盖/远程安装。改规则不动 Python 代码（`app/text2sql/IMPLEMENTATION.md` ④）。
- **"为什么用 sqlglot 而不是正则/字符串判断只读？"**
  - `WITH x AS (...) INSERT ...` 的**首词是 `WITH`**，字符串判断会误放行；sqlglot 解析后最外层是 `exp.Insert`（`WITH` 只挂在 `args["with"]`），据 AST 判最外层类型才能拦下（`IMPLEMENTATION-sql-guard.md` ②）。正则永远追不上 SQL 语法的边角。
- **"列名校验会不会误报把合法 SQL 拦下？"**
  - 会，所以策略是**保守到极致、宁漏报不误报**：只在"能唯一确定列归属"（单物理表、无 CTE、无子查询）时才校验非限定列；多表/子查询/CTE 一律跳过。误报会误导模型反复重写，代价高于漏报——漏报由引擎级 `mode=ro` 兜底（`IMPLEMENTATION-sql-guard.md` ④）。
- **"为什么 schema 全量注入，不做 embedding 召回？"**
  - demo 六张表全量约几百 token 完全可控；召回是为了"表多时省 token+提准确率"，此刻上向量库+预计算+阈值调参是**纯负担**。演进点写进了 `schema_search` docstring，需求到了（表数上量）再切，不提前抽象（`app/text2sql/IMPLEMENTATION.md` ④）。
- **"校验失败为什么返回中文错误而不是抛异常？"**
  - `execute_sql` 是给 LLM 用的工具，返回值回到模型上下文。返回"表 orders 不存在列 bad，可用列：…"这类带候选的中文报错，模型能据此**自纠改写**；抛异常会中断 Agent 循环，白丢自纠机会。SQLBot 是服务端 `raise`，场景不同（`IMPLEMENTATION-sql-guard.md` ④）。
- **"术语/示例为什么让模型主动调用，不 middleware 静默注入？"**
  - 静默注入（SQLBot 做法）省事但把上下文藏进 prompt，demo 时看不见、讲不清。做成显式 `sql_context_search` 工具后，每次命中的术语与示例都在工具轨迹里——**可观察、可讲解、可评估**（`IMPLEMENTATION-knowledge.md` ④）。

---

### 2.3 Agent 核心 + 工具熔断

**① 一句话**：用 langchain v1 `create_agent` + 中间件栈替换 my-agent 的手写 StateGraph；能力（Skills 披露/门控、工具熔断）全做成可插拔 `AgentMiddleware`；`LLMFactory` 解除厂商绑定。

**② 展开**：
- **组装点**（`app/core/dependencies.py::get_chat_agent`，进程级单例）：`ChatAgent(llm=LLMFactory.create_llm(), tools=base_tools, middleware=[SkillsMiddleware(...), ToolRuntimeMiddleware()])`。中间件顺序 `[Skills, ToolRuntime]` 有意为之——先 Skills 决定工具可见性/动态工具，再 ToolRuntime 包执行。
- **工具熔断**（`app/agents/tool_runtime.py`，原样保留自 my-agent）：每工具独立断路器三态——`closed`（正常）→ 连续失败达阈值 `open`（短路不再调）→ 过 `recovery_timeout` `half_open`（放一次探针，成功回 closed、失败回 open）。`TOOL_POLICIES` 按工具给差异化超时/重试；`_is_retryable_exception` 只对超时/连接类/502/503/504/429 瞬时错误重试。
- **降级回喂**（`ToolRuntimeMiddleware.awrap_tool_call`）：熔断打开/重试耗尽时**不抛异常**，而是把降级文案包成 `ToolMessage(status="error")` 回喂模型，Agent 降级续跑而非崩溃。用 `result_cell` 存 handler 真实返回（保住 Skills 的 `Command` 副作用），只把文本形式喂给失败启发式。

**③ 追问层**
- **"为什么从手写 StateGraph 换成 create_agent？"**
  - 手写图要自己维护节点/条件边/消息累加；而 Skills 的披露/门控/激活需要在"模型调用前"和"工具调用后"两个切面精确插手——这正是 `AgentMiddleware` 的 `awrap_model_call`/`awrap_tool_call` 钩子，手写 `call_tools` 节点很难干净表达（尤其动态工具的 `request.override`）。换 create_agent 后图交给框架，能力可插拔，还白得流式（`IMPLEMENTATION-agent-core.md` ④）。
- **"熔断为什么做成中间件，不包在工具函数里？"**
  - 熔断是横切关注点，做成中间件能**对所有工具统一生效**，包括 Skills 懒加载进来的动态 MCP 工具；工具函数保持纯粹、可单测。my-agent 把它焊在 `call_tools` 节点里，只覆盖那个 agent 自己的工具，换 agent 就得重写——中间件把这份能力解耦了。
- **"超时/重试参数怎么定的？一刀切吗？"**
  - 不一刀切。`TOOL_POLICIES` 按工具性质给：外部网络工具（`tavily_search`）12s + 1 次重试容忍抖动；纯本地极快的 `get_current_datetime` 2s 且 0 重试（快失败不拖延）；易抖的类给更低 `failure_threshold` 更快熔断。重试只认瞬时错误，业务性失败（工具返回 `success=false`）不做无谓重试。
- **"LLMFactory 解决了什么？为什么不用 ChatTongyi？"**
  - my-agent 把 `ChatTongyi(dashscope_api_key=...)` 硬编进 ChatAgent/ChatService/AIOpsService 好几处，换模型要改多处、还只能用通义。`LLMFactory.create_llm` 统一造 `ChatOpenAI`，model/api_key/base_url 全从 settings 读，于是 DashScope/美团 FRIDAY/本地 Ollama 任意 OpenAI 兼容接口都能接。一个刻意设计：**api_key 为空时构造期就 raise**，不让空 key 拖到第一次真实调用才 401（`IMPLEMENTATION-agent-core.md` ②）。
- **"降级回喂时怎么保证不破坏 Skills 的激活副作用？"**
  - 关键在 `result_cell`：把 handler 的**真实返回**（`ToolMessage`/`Command`，激活等副作用都在里面）存下来，只把它的**文本形式**喂给 `safe_tool_execute` 做失败判断。成功就原样返回真实结果（不破坏 `Command`），失败才替换成降级文案。

---

### 2.4 MCP 系统

**① 一句话**：把任意 MCP server 注册进平台，其工具经 langchain-mcp-adapters 转成 LangChain 工具；与 Skills 联动——**技能激活后才懒加载**对应 server 的工具，不激活一次连接都不发起。

**② 展开**（`app/mcp/service.py`）：
- **数据模型**：`MCPServer`（slug/name/transport(stdio|sse|streamable_http)/连接字段/enabled/disabled_tools），`to_client_config()` 按 transport 门控投影字段。
- **注册表**：JSON 文件原子写（tmp+rename），损坏时显式报错不静默清空。
- **load_tools**：并行 gather + 单 server 失败/超时隔离（返回 []）+ `asyncio.wait_for` 超时（默认 20s）+ 缓存（键 `slug:config_hash`，配置变即失效）。
- **联动**：`SkillsMiddleware` 在技能激活后 `load_tools(激活技能的 mcps)`，动态工具追加进 `request.tools`，并在 `wrap_tool_call` 里 `request.override(tool=实例)` 接管执行。

**③ 追问层**
- **"你说修正了 Yuxi 的问题，具体哪几个？"**（`app/mcp/IMPLEMENTATION.md` ③）
  1. **stdio 无超时会卡死请求**：Yuxi `get_tools()` 没有超时包裹，stdio 拉起的子进程挂起就卡死整个请求；本项目 `asyncio.wait_for` 包住，超时即降级返回 []。
  2. **disabled server 不可测**：Yuxi 测试/获取都过 `enabled==1` 过滤，启用前没法验证连不连得通；本项目 `test_server` 不要求 enabled，注册后立刻可测。
  3. **per-tool toggle 全量重连**：Yuxi 改 `disabled_tools` 却清整份工具缓存、被迫重连；本项目把 `disabled_tools` 排除在 `config_hash` 外 → 启停单个工具时连接缓存稳定，只过滤返回值。
  4. **enabled 用 Integer 1/0** 易错；本项目直接用 `bool`。
- **"动态 MCP 工具为什么又要 override？和门控工具的坑是一回事吗？"**
  - 同源。动态 MCP 工具**构建期不存在**、没进 ToolNode，执行时必须在 `wrap_tool_call` 里 `request.override(tool=实例)` 接管，否则同样被判 "not a valid tool"。门控工具是"构建期注册、请求期隐藏"，MCP 工具是"构建期没有、请求期注入并接管执行"——两种都源自 langchain v1 "ToolNode 只认构建期工具集"这一约束。
- **"为什么只做懒加载单路径，不做构建期全量注册？"**
  - 懒加载"不激活不连接"最省资源、最贴合按需披露的理念。Yuxi 有双路径（构建期全量 + 懒加载），本项目只做懒加载主路径，构建期全量配置按需二期补（`app/mcp/IMPLEMENTATION.md` ④）。
- **"MCP 的安全边界在哪？"**
  - `transport=stdio` 的 `command/args` **可执行任意命令**，MCP 配置面本质就是服务器命令执行面。当前 `get_current_user` 是占位鉴权，**生产开放该 API 前必须先落地真实鉴权**；多用户还需 http url 主机白名单、stdio 命令白名单。Yuxi 同样无 SSRF/命令白名单，靠 admin-only 兜底——这条边界两边都得靠部署侧闸门（`docs/openspec/mcp-system.md` §6）。

---

### 2.5 评估体系

**① 一句话**：两套评估共用一个理念——不靠感觉说"Agent 挺准"，而是拿数据集量化。Text-to-SQL 执行准确率 92.86%（26/28），另有从 my-agent 迁回的 RAG 检索/生成双评估。

**② 展开**（`evals/IMPLEMENTATION.md`）：
- **Text-to-SQL 评估**（`evals/text2sql/`）：流程 = M-Schema + sql-generation 技能正文组 prompt → LLM 生成 SQL → `validate_sql` 校验 → 执行 → 与 golden 结果集按 execution accuracy 对比。28 例按难度分桶（单表聚合/多表JOIN/时间过滤/TopN/CTE），报告落 `reports/execution_latest.json`。它把项目三块能力（Skills 即提示词模板/M-Schema/sqlglot 校验）串起来做端到端度量。
- **RAG 评估**（`evals/rag/`，迁回 my-agent）：检索指标 Hit@k/Recall@k/MRR/NDCG/MAP，生成指标关键词召回/来源命中/严格版事实点召回，带 `baselines/` 做基线对比（调参后看涨跌）。

**③ 追问层**
- **"为什么比结果集不比 SQL 文本？"**
  - 同一问题的正确 SQL 有无数写法（别名/JOIN 顺序/子查询 vs CTE/ROUND 与否）。文本相似度（编辑距离/BLEU）会把语义等价的不同写法判错、把"长得像但跑出来不同"的判对——与"答案对不对"几乎无关。执行准确率直接对齐业务目标：**用户要的是正确数据，不是正确字符串**（`evals/IMPLEMENTATION.md` §四.1）。
- **"结果集对比怎么做归一化？不会把错的判对吗？"**
  - 吸收三类无关差异：①列序无关（每行单元格按稳定 key 排序后比）②行序——**仅当 golden 无 ORDER BY 时**按多重集合（Counter）比，保留重数；golden 有 ORDER BY 则逐行有序比 ③浮点容差（金额/均值量化到固定小数位）。代价是取了刻意简化：极端下 `(1,2)` 与 `(2,1)` 会判等——demo 业务查询里几乎不出现同类型可互换列，可接受（`evals/IMPLEMENTATION.md` §四.2）。
- **"哪类查询最弱？为什么？"**
  - CTE 75%（3/4）、时间过滤 80%（4/5）最弱，单表聚合 100%。两个失败例都出在**排序稳定性**：TopN 并列时模型漏了 tiebreaker（见 §3-⑧）。分桶报告的作用就是定位薄弱项、指导 prompt 调优。
- **"为什么离线测试（pytest）不调 LLM？"**
  - LLM 输出不确定、依赖网络与密钥、有 token 成本，进不了 CI、无法稳定复现。所以 `tests/test_text2sql_eval.py` 只测**确定性**部分（golden 能跑通、归一化对比正反例、报告聚合），把"模型多准"留给 `run_execution_eval` 这个需要真实 LLM 的线下脚本。测试守护"评估工具本身正确"，脚本产出"模型准确率数字"（`evals/IMPLEMENTATION.md` §四.3）。
- **"这个 92.86% 可信吗？会不会数据集是你挑的简单题？"**
  - 数据集**难度分层**、每条 golden 都先在合成库真实执行验证过且保证有结果（离线测试逐条守护）；分桶让弱项无处可藏（CTE 就是明显低）。可信度来自"口径公开（execution accuracy）+ 数据集可复现（合成库固定种子）+ 失败例可复盘"，而不是一个孤立数字。
- **"RAG 评估的 baseline 对比是怎么用的？"**
  - `evals/rag/baselines/` 存了一份检索/生成的基线结果。改检索参数（`top_k` / 是否 hybrid / 是否 rerank）后重跑，与基线逐指标比涨跌——这样"调参有没有用"是有数据支撑的，而不是凭感觉。指标用 Hit@k / Recall@k / MRR / NDCG / MAP（检索）和关键词召回 / 来源命中 / 严格版事实点召回（生成），全部沿用自 my-agent 的 `metrics.py`（原样迁回未改）。Text-to-SQL 评估就是这套"检索/生成评估范式"向 SQL 域的延伸：把"命中率"换成"执行准确率"，把"gold_sources"换成"golden 结果集"。
- **"为什么不用 LLM 自动生成评测集（像 Yuxi 那样）？"**
  - Yuxi 用 LLM 从知识库 chunk 自动生成问答对，免人工标注、可规模化——那是它知识库体量大的刚需。本项目 demo 规模小，`dataset.json` 走**人工精标 + 真实执行校验**更可控、更可讲（每条都跑得通、有结果）。但"用 LLM 批量造 question→SQL 对"是明确的扩展方向，且与 SQL 示例库（P1-3）相通（`evals/IMPLEMENTATION.md` §三）。

---

### 2.6 工程实践（并行开发 · 测试）

**① 一句话**：10 个 PR 的开发史、113 个 pytest 用例、中文 Conventional Commits；三个特性分支并行开发、合并期用集成测试兜住分支间的交互 bug。

**② 展开**：
- **分支策略**：每个特性一个分支一个 PR（`feat/skills-v2-mcp`、`feat/text2sql-core`、`feat/sql-guard`、`feat/demo-data`、`feat/evals-revive`、`feat/sql-knowledge`、`feat/web-frontend` 等），合并入 `main` 前跑测试。
- **测试分层**：11 个测试文件覆盖 Skills（加载/链式/菱形/真环/中文匹配/目录导入）、中间件端到端（假模型驱动真实 `create_agent`：披露不含正文、门控前后可见性、激活入 state）、MCP（真实 FastMCP stdio server 端到端）、SQL 校验、知识库、Text-to-SQL 评估工具。
- **文档化**：每个模块一份 `IMPLEMENTATION*.md`（功能/原理/参考项目怎么做/区别取舍/遗留），设计规格走 OpenSpec（`docs/openspec/`）。

**③ 追问层**
- **"三个分支各自测试全绿，合并后就没问题了吗？"**
  - 不是。**各分支全绿 ≠ 合并后全绿**。`feat/sql-guard` 单独测校验层没问题，但和 `feat/demo-data` 合并后，集成测试抓到了 `SELECT ... AS n ... ORDER BY n` 的别名误报（PR #5，见 §3-④）。教训：分支交互处必须有集成测试兜底，单元测试的绿是局部的。
- **"你的测试有没有测出过'假通过'？"**
  - 有，而且是深刻一课：早期 `test_skills.py` 只 `print` 返回值、**无断言**，中文分词失效返回空列表时"什么都不打印"和"通过"看起来一样，把一个 P0 缺陷（`auto_match=True` 等价于不挂任何技能）掩盖了（见 §3-①）。所以现在强调断言、强调测试要能区分"匹配到 0 个"和"匹配正确"。
- **"为什么用中文 Conventional Commits？"**
  - 团队/项目是中文语境，commit message 是给人读的追溯凭证。类型前缀（feat/fix/docs/refactor/style/chore）保留英文规范便于工具解析，正文用中文讲清"改了什么、为什么"。每个 fix commit 都写了根因+修复+回归测试（如 `7b9f04d`、`a44af5f`），本身就是踩坑记录。
- **"113 个测试，覆盖率够吗？哪里最薄？"**
  - 诚实说：**初始化路径和分支交互处**是历史薄弱点——死锁 bug（§3-⑤）正是因为 `get_chat_agent` 这条路"冒烟只走了路由直调 `get_skill_service`"从未被覆盖。补测的原则是"每修一个 bug 加一条回归用例"，让盲区逐个变成红线。

---

## §3 八个踩坑故事（完整版）

> 讲法：背景 → 现象 → 定位 → 修复 → 教训。每个都能指到 commit/PR/文件。诚实讲坑比吹功能更能加分。

### ① 中文分词 `str.split()` 失效，且被无断言 print 测试掩盖

- **背景**
  - Skills 系统有个 `match_skills_by_query`，想按用户的中文问题自动匹配相关技能，作为渐进披露之外的一处主动增强（Yuxi 本身没有这一层，是本项目的增量）。
  - 期望：问"查询数据库表结构"能匹配到 `schema-retrieval`，问"制作图表"能匹配到 `data-visualization`。
- **现象**
  - 实测几乎全部返回空：`'查询数据库表结构' -> []`、`'制作图表' -> []`、`'帮我看下上个月各州的销售额' -> []`。
  - 唯一"命中"的是 `'生成 SQL 查询' -> [('sql-generation', 1)]`——但那纯属巧合，因为句子里的 "sql" 恰好被空格分开成了独立 token。
- **定位**
  - `service.py:413-416` 用 `str.split()` 分词。中文句子**没有空格**，整句被切成**一个 token**，与技能描述的词元集合求交集恒为空。
  - 另两条打分规则同样失效：`skill.slug in query`（要求用户原话里出现 "schema-retrieval" 这种 slug）、`skill.name.lower() in query`（name 是英文）在中文场景恒不命中。
  - **最坑的一环在测试**：`test_skills.py` 只 `print` 返回值、**没有任何断言**。返回空列表时"什么都不打印"，和"匹配正确"在输出上看起来一模一样。于是"测试通过：语义匹配工作"这个结论根本不成立——当时 `auto_match=True` 等价于**不挂载任何技能**。
- **修复**
  - 分词改用 `jieba`（`app/skills/service.py::_tokenize`：jieba 切分 + 小写 + 过滤单字符标点）。
  - 给测试加真实断言并迁到 `tests/`，让它能区分"匹配到 0 个"和"匹配正确"。
  - SQL 示例库检索（`ExampleStore.search`）复用同一套 `_tokenize`，保证全项目中文切分口径一致。
- **教训**
  - 中文处理不能想当然套英文那套以空格为界的分词。
  - **没有断言的测试是负资产**——它给你虚假的安全感，比没有测试更危险，因为它让你误以为这块已经被守护了。
- **佐证**：`docs/openspec/skills-optimization.md` §3.1；`app/skills/service.py::_tokenize`；`app/text2sql/IMPLEMENTATION-knowledge.md` §2.1。

### ② 菱形依赖被误报为循环依赖（全局 visited vs 分支 stack）

- **背景**
  - 技能依赖需要递归展开：一个技能声明 `dependencies.skills`，展开时要把传递依赖都拉进披露列表。
  - 必须判环，否则 `A→B→A` 这种真环会导致无限递归。
  - 典型的**菱形依赖**：A 依赖 B、C，而 B、C 又都依赖 D（`A→B→D`、`A→C→D`）。
- **现象**
  - 对上面的菱形，展开**结果是对的**（D 只入一次，顺序 `['a','b','c','d']`）。
  - 但日志持续报告 `检测到循环依赖，跳过: d`——把一个完全正常的**共享依赖**误报成了循环依赖。
  - 结果虽对，但满屏假告警会淹没真正的环告警，也让人怀疑展开逻辑有问题。
- **定位**
  - `service.py:343` 用了**一个全局 `visited` 集合**同时干两件事。
  - D 先被 B 这条分支访问、进了 `visited`；C 分支再访问 D 时命中 `visited`，就被当成"环"。
  - 根因：**判环**需要的是"当前 DFS 路径（从根到当前节点）上是否重复"，这是 `stack` 语义；**去重**需要的是"全局是否已经展开过"，这是 `seen` 语义。两者语义不同却被塞进了同一个集合。
- **修复**
  - 两把锁分开（对齐 Yuxi 的做法）：`stack`（当前 DFS 路径的副本）命中 = **真环**，告警并跳过（`A→B→A`）；`seen`（全局已展开）命中 = **菱形依赖**，静默去重、不告警（D 只展开一次）。
  - 当前三个内置技能恰好是**链式**（`data-visualization → sql-generation → schema-retrieval`），所以线上没触发；但菱形是迟早会遇到的，且已用测试固定住（真环告警 / 菱形不告警各一条用例）。
- **教训**
  - **判环 ≠ 去重**，在 DFS 里这是两件事，要用两个数据结构（路径 stack + 全局 seen）。
  - 照抄参考实现时要理解它"为什么这么分"，而不是看着"两个集合好像冗余"就简化成一个——简化掉的往往正是关键区别。
- **佐证**：`docs/openspec/skills-optimization.md` §3.2；`app/skills/IMPLEMENTATION.md`「依赖展开：菱形去重 vs 真环告警」；`docs/openspec/skills-system.md` §4。

### ③ langchain v1：门控工具必须构建期注册，动态 MCP 工具必须 wrap_tool_call override

- **背景**
  - Skills 门控要做到"未激活时工具对模型不可见、激活后才可见"（如 `execute_sql` 要等 `sqlite-query` 激活）。
  - MCP 要做到"技能激活后才懒加载对应 server 的工具"（如 `chart-mcp` 的图表工具）。
  - 直觉做法：在中间件的请求期回调里，按需往 `request.tools` 里增删工具。
- **现象**
  - 早期把门控工具只在**请求期**塞进/移出 `request.tools`。模型发起调用时，执行器直接报 **"not a valid tool"**。
  - 动态 MCP 工具同样：追加进 `request.tools` 后模型能"看到"，但一调用就报同样的错。
- **定位**
  - langchain v1 的 `ToolNode` 在 **构建期**（`create_agent` 建图时）就固定了"可执行工具集合"。
  - 请求期的中间件只能改**可见性**（模型能不能看到，即 `request.tools` 过滤），**改不了可执行性**（执行器认不认，即 ToolNode 的工具表）。
  - 门控工具当时没在构建期注册进 `middleware.tools`，ToolNode 里根本没有它；动态 MCP 工具构建期压根不存在。Yuxi 的 `skills.py:151-164` docstring 白纸黑字写了这个坑。
- **修复**
  - **门控工具**：`SkillsMiddleware.__init__` 里 `self.tools = list(gated_tools)`，**构建期全量注册**进 ToolNode；请求期只算 `hidden = (declared & gated) - unlocked` 从 `request.tools` 过滤，**只动可见性、从不动可执行性**。
  - **动态 MCP 工具**：构建期不存在，只能在 `wrap_tool_call` 里 `request.override(tool=实例)` 接管这次执行。
  - `app/skills/middleware.py` 顶部注释直接标注"Yuxi skills.py:151-164 同款坑"，防止后人再踩。
- **教训**
  - **可见性与可执行性是两个正交维度**，别指望用一个机制同时管。
  - 踩坑根因是一开始把 middleware 当成"能自由改 request 的回调"，没吃透框架"构建期定图、请求期只调参"的契约。读框架要先读它的不变量。
- **佐证**：`docs/openspec/skills-system.md` §2；`app/skills/middleware.py`；`app/mcp/IMPLEMENTATION.md` ①；`app/core/dependencies.py:168-173`（gated_tools 构建期注册）。

### ④ 三个并行分支各自全绿，合并后集成测试抓到 SELECT 别名误报

- **背景**
  - P1 阶段三个特性分支并行开发：`feat/sql-guard`（SQL 校验层）、`feat/demo-data`（演示数据）、`feat/text2sql-core`（M-Schema）。
  - `feat/sql-guard` 的 sqlglot 校验层做"表列存在性校验"，分支内单测全绿。
- **现象**
  - 分支合并后，`feat/demo-data` 的集成测试红了：`SELECT COUNT(*) AS n FROM customers GROUP BY customer_state ORDER BY n` 被误报"未知列 n"，合法查询被拦下。
- **定位**
  - `n` 是 `SELECT` 子句里 `AS` 定义的**输出列别名**，不是任何物理表的列。
  - 校验非限定列时，只拿了物理表的列集合去比对，**没把 SELECT 里定义的别名算进"合法列"**，于是 `ORDER BY n` 里的 `n` 被当成未知列。
  - 关键洞察：这和之前 CTE 别名被误当物理表是**同一类问题**——都是"把某种别名当成了不存在的物理列/表"。别名的种类（CTE 名、SELECT 输出列名、派生表名）都要在校验前先排除。
- **修复**（PR #5 / commit `a44af5f`）
  - 收集 `exp.Alias` 的别名集合，校验非限定列时命中别名即跳过（"宁漏报不误报"的原则不变——漏报由引擎级 `mode=ro` 兜底）。
  - 加回归用例 `test_select_alias_in_order_by_not_false_positive` 固定住。
- **教训**
  - **各分支各自全绿 ≠ 合并后全绿**：单元测试的"绿"是局部的，分支交互处（校验层 × 真实数据/真实查询）必须有集成测试兜底。
  - 这个 bug 单看 `feat/sql-guard` 永远发现不了——是 `feat/demo-data` 的真实查询把它照出来的。所以我在合并节点专门跑跨分支集成测试。
- **佐证**：commit `a44af5f`（PR #5，`fix/sql-guard-select-alias`，commit message 明确写了"三个并行分支各自全绿，合并后 feat/demo-data 的集成测试抓到此问题"）；`app/agents/tools/IMPLEMENTATION-sql-guard.md`；`evals/IMPLEMENTATION.md` §四.4（离线测试为此改用原生 sqlite3 跑 golden，不被这条待修 bug 干扰）。

### ⑤ asyncio.Lock 不可重入 → get_chat_agent 初始化死锁（测试盲区）

- **背景**
  - `get_chat_agent` 是进程级单例组装点，用一把 `_init_lock`（`asyncio.Lock`）做初始化并发保护（双检锁：锁外判一次、锁内再判一次）。
  - 它内部要先拿到 `skill_service`，于是调用 `get_skill_service` 来加载内置技能。
  - 而 `get_skill_service` **自己也拿同一把 `_init_lock`** 做它的初始化保护。
- **现象**
  - **首次 chat 请求永久挂起**（观测到 5 分钟+仍不返回），此后所有请求全卡死。
  - 诡异的是单独测 `get_skill_service` 一切正常，只有走 chat 这条链才挂。
- **定位**
  - `get_chat_agent` 在**持有 `_init_lock` 的情况下**调用了 `get_skill_service`，后者又试图 `acquire` 同一把锁。
  - `asyncio.Lock` **不可重入**——同一协程第二次 `acquire` 会永远等待自己释放，直接死锁。
  - 更隐蔽的是**测试盲区**：这条组装路径此前**从未被测试覆盖**，冒烟测试只走了"路由直调 `get_skill_service`"，绕开了 `get_chat_agent → get_skill_service` 这条嵌套加锁链，所以 bug 一直潜伏到首次真实 chat 才爆。
- **修复**（PR #10 / commit `7b9f04d`）
  - 把 skill/mcp 单例的初始化**移到 `_init_lock` 之外**先完成（它们各自内部已有并发保护），再进锁做 agent 组装。
  - 加回归测试 `tests/test_dependencies.py`，用 `asyncio.wait_for(..., 10s)` 守护整条初始化路径，一旦再死锁就超时判失败。
  - 代码里留了醒目注释（`dependencies.py:126-128`）解释"为什么必须在拿锁前完成"。
- **教训**
  - `asyncio.Lock` 不可重入：持锁函数调用的任何下游，都不能再去拿同一把锁；共享锁要么改可重入语义，要么把嵌套调用挪出临界区。
  - **没被测试走过的路径 = 定时炸弹**。单例组装这种"只在进程首次请求时触发一次"的路径，最容易成为盲区，必须专门补测。
- **佐证**：commit `7b9f04d`（PR #10，`fix/agent-init-deadlock`，main 合并后测试数 113→115）；`app/core/dependencies.py:120-133`；`tests/test_dependencies.py`。

### ⑥ LLM 无超时，端点不可达时挂满 5 分钟 → 显式超时 + 快速失败

- **背景**
  - P0-4 真实 LLM 端到端联调，配置的是美团 FRIDAY 端点（`base_url` 指向内网 endpoint）。
- **现象**
  - FRIDAY 端点在当时环境不可达（需内网/VPN），一次 chat 请求**挂满约 5 分钟**才失败。
  - 联调体验极差，而且这种长挂起会顺带拖住上层的 FastAPI 请求与调试循环。
- **定位**
  - `LLMFactory.create_llm` 构造 `ChatOpenAI` 时**没有显式配置 timeout**，底层 HTTP 客户端用了很长的默认超时。
  - 端点连不上（TCP 层不可达）时，客户端就一直干等到默认超时才放弃——对交互式联调来说等于卡死。
- **修复**（commit `9e9f2577`）
  - `ChatOpenAI` 显式传 `timeout=settings.llm_request_timeout`（默认 **60s**，可通过 `.env` 配）+ `max_retries=2`。
  - 端点不可达时**快速、显式地失败**，而不是无声地挂满默认超时。
  - `settings.py` 新增 `llm_request_timeout: float = 60.0`，代码注释直接写"曾因无超时挂满 5 分钟"。
- **教训**
  - **任何跨网络调用都必须有显式超时**，永远不要信底层库的默认值——它们通常为吞吐而非交互式体验而设。
  - 这和 MCP `load_tools` 用 `asyncio.wait_for` 包 `get_tools()`（§2.4）、以及工具熔断的 per-tool 超时（§2.3）是**同一条纪律**：外部依赖一律"快速失败优于无限等待"。面试时可以把这三处串成一个"超时纪律"主题讲。
- **佐证**：commit `9e9f2577`（`fix(llm): LLMFactory 增加显式超时与重试`，该提交曾因 #5 先被合并而成为孤儿提交，后经文档溯源发现、已 cherry-pick 进 PR #10 —— 这个'账目错误被溯源抓住'的过程本身也是可讲的工程故事）；`app/core/settings.py::llm_request_timeout`；`app/core/llm.py`（`timeout` / `max_retries` 参数）。

### ⑦ FastAPI 依赖遮蔽：路由本地 get_skill_service 覆盖单例 → 列表恒空

- **背景**
  - `/api/skills` 系列端点用 FastAPI 的 `Depends` 依赖注入拿 `SkillService`。
  - 正确的来源是 `core/dependencies.py::get_skill_service`——那个会在首次调用时加载全部内置技能的**单例**。
- **现象**
  - `GET /api/skills` **返回空列表**，前端 Skills 页一片空白。
  - 但后端日志显示内置技能明明加载成功了，chat 链路也能正常用技能——只有这个列表接口是空的。
- **定位**
  - `routes_skills.py:80-83` 自己**又定义了一个同名的** `get_skill_service`，返回的是**裸 `SkillService()`**——一个没有加载任何内置技能的全新空实例。
  - 路由上的 `Depends(get_skill_service)` 按**函数对象**解析，用到的是模块内这个局部函数，**静默遮蔽**了 `core/dependencies` 的单例。
  - 所以每次请求都新建一个空 service，列表自然恒空——而且不报任何错。
- **修复**
  - 删掉路由文件里的局部 `get_skill_service`，统一 `from app.core.dependencies import get_skill_service`。
  - 在规格里立规矩：**路由文件内不得自定义与 `core/dependencies` 同名的依赖函数**（`skills-system.md` §6）。
- **教训**
  - FastAPI 的 `Depends` 是按**函数对象身份**解析的，同名局部函数会静默遮蔽你想要的全局单例，不报错、只给你一个"看起来对但其实是空的"对象——这类 bug 最难查，因为没有异常、没有栈。
  - **依赖注入的单例必须有唯一来源**，别在路由里图省事 `SkillService()` 重新 new 一个。
- **佐证**：`docs/openspec/skills-optimization.md` §3.3（`routes_skills.py:80-83`）；`docs/openspec/skills-system.md` §6（"依赖注入统一走 `get_skill_service` 单例，路由文件内不得自定义同名依赖 —— v1 曾因遮蔽导致列表恒空"）。

### ⑧ TopN 并列导致 execution accuracy 误判（评估集设计课）

- **背景**
  - Text-to-SQL 评估里有 TopN 题（`ORDER BY ... LIMIT N`）。这类题**行序敏感**：评估约定是"golden 有 ORDER BY 时逐行有序比较"。
- **现象**
  - `t2s_021`"下单次数最多的前 10 位客户"判错，但模型生成的 SQL 单看**完全合理**，跑出来也是"下单最多的客户"。
- **定位**
  - golden：`... ORDER BY order_count DESC, c.customer_unique_id LIMIT 10`——带了**次级排序键（tiebreaker）** `customer_unique_id`。
  - 模型生成：`... ORDER BY order_count DESC LIMIT 10`——**没有 tiebreaker**。
  - 当第 10 名、第 11 名的 `order_count` **并列**时，两条 SQL 在并列组内的取舍不同，取到的第 10 行不一样，结果集不一致 → 判错。
  - 这不完全是模型的错：TopN 在"边界并列"上**本就有歧义**，golden 补了 tiebreaker、模型没补，双方都"对"但结果不同。
- **修复 / 记账**
  - 这是**评估集设计的已知取舍**，不是一个待修 bug：行序敏感性刻意绑定"golden 是否有 ORDER BY"（因为 TopN 的排序本身就是考点，不能忽略行序）。
  - 两条出路：要么 golden 全部补齐 tiebreaker 且在提示词里要求模型也补；要么承认边界并列是 TopN 的固有歧义、接受这类扣分。当前选择后者，把它留在分桶报告里作为**薄弱项定位**（`t2s_021` 同时压低了 TopN 87.5% 和多表JOIN 91.7% 两个桶）。
- **教训**
  - **评估集本身也会有 bug 或歧义**。"准确率没到 100%"不一定是模型差，可能是评估口径没把边界情况（如并列）定义清楚。
  - 做评估要有"元评估"意识：每个失败例都要判一次"是模型真错了，还是 golden/口径的期望本身有歧义"，否则会被虚低的数字误导去优化错误的方向。
- **佐证**：`evals/text2sql/reports/execution_latest.json`（`t2s_021` 多表JOIN+TopN、`t2s_027` CTE+时间过滤两个失败例）；`evals/IMPLEMENTATION.md` §四.2（行序敏感性绑定 golden ORDER BY 的约定与归一化取舍）。

---

## §4 数字速记卡

> 面试前扫一眼，数字张口就来。所有数字均可溯源。

| 主题 | 数字 | 出处 |
|---|---|---|
| pytest 用例（main） | **113 passed**（11 个测试文件） | 本仓库 `tests/`，实测 |
| 死锁修复合并后 | **115**（PR #10 / `7b9f04d`） | commit `7b9f04d` |
| Text-to-SQL 执行准确率 | **92.86%（26/28）**，模型 qwen3.7-max | `evals/text2sql/reports/execution_latest.json` |
| 分桶：单表聚合 | 100%（14/14） | 同上 |
| 分桶：多表 JOIN | 91.7%（11/12） | 同上 |
| 分桶：TopN | 87.5%（7/8） | 同上 |
| 分桶：时间过滤 | 80%（4/5） | 同上 |
| 分桶：CTE | 75%（3/4） | 同上 |
| 评估耗时 | 231.2s / 28 例 | 报告 meta |
| 端到端单轮（真实 LLM） | **约 13.4s**：read_skill 激活 → schema_search → execute_sql → 表格回答 | 联调实测 |
| 渐进披露：全文注入 | ~1183 tokens/请求（3 技能 2366 chars，无法关闭） | `skills-optimization.md` §2.2 |
| 渐进披露：名称+描述 | **~150 tokens/请求**（4 内置技能，与正文长度无关） | `skills-system.md` §2 |
| 内置技能 | 4 个（schema-retrieval / sql-generation / sqlite-query / data-visualization） | `app/skills/buildin/` |
| 演示库 | 6 张表（orders/order_items/customers/products/sellers/payments） | `scripts/import_ecommerce.py` |
| 开发史 | 10 个 PR（#1–#9 入 main，#10 待合并），中文 Conventional Commits | `git log` |
| 核心版本 | langchain 1.3.14 / langgraph 1.2.9 / FastAPI 0.139.2 / sqlglot 30.13 / Vue 3.4 | 实测已安装版本 |
| MCP 修正 Yuxi 问题 | 4 个（stdio 超时 / 禁用可测 / 缓存不重连 / bool） | `app/mcp/IMPLEMENTATION.md` ③ |
| LLM 超时 | 60s + 2 retries（曾无超时挂 5 分钟） | PR #10（cherry-pick 9e9f257）|

---

## §5 简历句 → 证据映射

> 简历上的每一句都要能落到具体文件/commit。被追问时直接指过去。

| 简历句 | 支撑证据 |
|---|---|
| "基于 langchain v1 `create_agent` + 中间件架构实现 Skills 插件系统（渐进式披露/激活门控），上下文注入成本与技能数量解耦" | `app/skills/middleware.py`、`docs/openspec/skills-system.md`；数字 1183→150 tokens（`skills-optimization.md` §2.2） |
| "实现 MCP 标准化工具接入，技能激活后懒加载外部工具" | `app/mcp/service.py::load_tools`、`docs/openspec/mcp-system.md` §5；修正 Yuxi 4 个问题（`app/mcp/IMPLEMENTATION.md` ③） |
| "Text-to-SQL 全链路：M-Schema + 分层提示词 + sqlglot 校验 + 引擎级只读" | `app/text2sql/m_schema.py`、`sql-generation/SKILL.md`、`app/agents/tools/sql_guard.py`；`IMPLEMENTATION-sql-guard.md` |
| "建立检索与生成双评估体系，SQL 执行准确率 92.86%" | `evals/text2sql/` + `evals/rag/`、报告 `execution_latest.json`；`evals/IMPLEMENTATION.md` |
| "SQL 示例库 + 术语库构建'越问越准'知识闭环" | `app/text2sql/examples.py`/`terminology.py`、`sql_context_search` 工具；`IMPLEMENTATION-knowledge.md` |
| "工具调用熔断/降级机制，外部依赖故障时 Agent 降级续跑" | `app/agents/tool_runtime.py`（三态断路器）、`ToolRuntimeMiddleware`；`IMPLEMENTATION-agent-core.md` ② |
| "LLM 抽象层解除厂商绑定，支持任意 OpenAI 兼容接口" | `app/core/llm.py::LLMFactory`；`REQUIREMENTS.md` §3.3 |
| "Skills 支持携带可执行脚本（目录模型）+ 远程安装（默认禁用兜安全）" | `sqlite-query/scripts/query.py`、`run_skill_script`、`remote_install.py`（`enabled=False`） |
| "Vue3 前端：对话（SSE 流式）+ Skills/MCP 管理页" | `frontend/src/views/`；`frontend/IMPLEMENTATION.md`（SSE 跨 read 缓冲修正） |
| "10 个 PR、113 pytest 用例、中文 Conventional Commits 的工程化开发" | `git log`；`tests/`（11 文件）；每个 fix commit 含根因+回归用例 |

---

> 收尾话术：这个项目我最想传达的不是"功能多"，而是**每个设计都有取舍、每个取舍都能追问、每个坑都留了记录**。想深入拷问任一子系统，可以用配套的 `/mock-interview` 技能。
