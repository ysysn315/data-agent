# Skills v2 系统速读（目录模型 + 渐进式披露 + 激活门控）

> 设计规格见 [docs/openspec/skills-system.md](../../docs/openspec/skills-system.md)（本文不重复其表格，
> 只做"代码怎么落地 + 为什么这么选"的实现速读）。分支来源：feat/skills-v2-mcp。
> 定位：把 Yuxi 的三段式技能机制（披露 → 激活 → 解锁）搬进本项目，让"能力 = 一段提示词模板 + 可选门控工具"，
> 不激活不占 system prompt、不解锁不进模型视野。

核心文件：

- `models.py` —— SKILL.md 解析（`SkillContent.parse`）、`Skill`（含 `dir_path`）、`ExpandedSkills`（披露/门控投影）
- `service.py` —— 加载 / 查询 / 依赖展开 / jieba 中文匹配 / 目录原子导入
- `tools.py` —— `read_skill`（激活入口）/ `run_skill_script`（subprocess + 路径校验 + 超时）
- `middleware.py` —— `SkillsMiddleware`（真实 langchain v1 `AgentMiddleware`，三段式主实现）
- `remote_install.py` —— git clone 整目录安装，默认 `enabled=False`
- `buildin/` —— 4 个内置技能（schema-retrieval / sql-generation / data-visualization / sqlite-query）

## ① 功能与用法：一次"各州销售额"跑通三段式

用户问"各州销售额 Top5"，一条链路走完三段：

1. **披露**（`awrap_model_call`）：system prompt 被追加一段技能清单，只有名称 + 描述 + "先调用
   `read_skill(slug)`"（见 `ExpandedSkills.build_system_prompt`）。正文一个字都没进 prompt，
   注入成本与 SKILL.md 长度无关。
2. **激活**：模型判断要查库，调用 `read_skill("sqlite-query")` → `tools.py` 里的工具返回该技能
   SKILL.md 全文；`SkillsMiddleware._maybe_activate` 拦截这次调用结果，把 `sqlite-query` 写进
   `state.activated_skills`（走 `Command(update=...)`）。
3. **解锁**：下一次模型调用时，`awrap_model_call` 发现 `sqlite-query` 已激活，它声明的
   `dependencies.tools: [execute_sql]` 从"隐藏名单"移除 —— `execute_sql` 这才对模型可见，模型据此
   生成 SELECT 并执行。未激活前，`execute_sql` 虽已构建期注册进 ToolNode，但被 `awrap_model_call`
   从 `request.tools` 过滤掉，模型根本看不到。

一个技能是**一个目录**（不是一个字符串）：根 `SKILL.md`（YAML frontmatter + 正文）+ 可选
`scripts/`（随附脚本，对标 Yuxi 的 mysql-reporter）。`sqlite-query` 就带了 `scripts/query.py`，
可经 `run_skill_script` 执行以演示"技能携带可执行脚本"的能力。

依赖在 frontmatter 里声明三类：`tools`（本地门控工具）、`mcps`（MCP server，激活后懒加载其工具，
见 [app/mcp/IMPLEMENTATION.md](../mcp/IMPLEMENTATION.md)）、`skills`（其他技能，递归展开进披露列表）。

## ② 实现原理与关键技术

### 目录模型与"读最新"

`Skill.dir_path` 指向技能目录，`content` 只是根 SKILL.md 的缓存（供 API 详情展示）。
`get_skill_body` 激活时**优先从目录现读** SKILL.md（`service.py`），改了文件即时生效，
无目录才回退 `content` 缓存。

### awrap_model_call：披露 + 门控 + MCP 懒加载三合一

- **激活状态收敛**：`activated &= {本次挂载闭包内的技能}` —— 只认当前请求闭包内的激活，避免跨请求串味。
- **工具门控算法**：`hidden = (declared & self._gated_tool_names) - unlocked`。`declared` 是展开后所有
  技能声明的工具，`unlocked = tools_of(activated)` 只取**已激活**技能的**直接**声明。隐藏的从
  `request.tools` 过滤掉。关键约束：门控工具必须构建期挂在 `middleware.tools` 上（`__init__` 里
  `self.tools = list(gated_tools)`），否则 langchain v1 的 ToolNode 执行时报 "not a valid tool"；
  请求期只调可见性，从不动可执行性。
- **MCP 懒加载**：已激活技能的 `mcps_of(activated)` 交给 `mcp_service.load_tools`，返回的动态工具
  追加进 `model_tools`，并登记到 `self._mcp_tools`（供 `wrap_tool_call` 接管执行）。

### awrap_tool_call / _maybe_activate：激活拦截

`_maybe_activate` 只认 `read_skill` 这一个工具名：取出 `slug`，若返回是"技能不存在"开头则不激活；
否则把 slug 合并进 `activated_skills`（`Command` 结果就地 merge，`ToolMessage` 结果包一层 `Command`）。
同步 / 异步两个 `wrap_tool_call` 都先做 MCP 动态工具的 `request.override(tool=实例)`，再走激活合并。

### 依赖展开：菱形去重 vs 真环告警（expand_dependencies）

递归**只沿 `skills` 边**；`tools`/`mcps` 只取直接声明、不递归。判环两把锁：

- `stack`（当前 DFS 路径）命中 = **真环**，告警跳过（`A→B→A`）；
- `seen`（全局已展开）命中 = **菱形依赖**，静默去重、不误报为环（`A→B→D` 与 `A→C→D`，D 只展开一次）。

这是相对 v1 的关键修正：早期只用一个 visited 集合，会把菱形依赖误当环报警。

### 目录原子导入（import_skill_dir）

`copytree` 到临时目录 → `rename` 到最终位置（原子）→ 入库；入库失败则 `rmtree` 回滚，
保证文件系统与存储层一致，不留半个损坏技能。删除时 `resolve().relative_to(save_dir/skills)` 校验，
只删 `save_dir` 管辖内的目录，绝不误删内置技能目录。

### 远程安装默认禁用的安全逻辑

`run_skill_script` 是**无隔离 subprocess**（REQUIREMENTS §9 决策，不做重沙箱），防护仅三层：
脚本路径必须落在 `<skill>/scripts/` 内（`relative_to` 校验拦路径穿越）、30s 超时、8000 字输出截断。
正因执行无隔离，`remote_install.py` 的 `_import_one` 强制 `enabled=False` —— 远程技能可能夹带
可执行脚本，必须人工审查内容后经 API 显式启用。远程安装走 `git clone --depth 1`，不依赖任何外部 CLI。

## ③ Yuxi 是怎么做的（对照 yuxi-reference）

`yuxi-reference/backend/package/yuxi/agents/middlewares/skills.py`：

- **披露注入**：`awrap_model_call`（219 行起）用 `append_to_system_message` 把技能段拼进 system message，
  与本项目一致；门控注释也一样强调"剔除只影响模型可见性、不影响可执行性"。
- **read_file 路径拦截激活**：`_process_tool_call_result` 只认 `read_file` 工具，
  `_extract_skill_slug_from_skill_md_path` 从 `/home/gem/skills/<slug>/SKILL.md` 这样的沙箱路径解析出 slug
  再激活。本项目没有沙箱文件系统，改用**专用 `read_skill` 工具**替代（工具即边界，天然只能读技能）。
- **构建期注册门控工具的坑**：`resolve_skill_gated_tools`（skills.py:149-164）的 docstring 白纸黑字写明
  "必须在构建期注册进 create_agent 的 ToolNode，否则即便模型发起调用，执行器也会判定为 not a valid tool"。
  本项目 `middleware.py` 顶部注释直接标注"Yuxi skills.py:151-164 同款坑"，用 `self.tools = list(gated_tools)`
  复刻了这个规避。

`yuxi-reference/.../agents/skills/service.py:668-717`（`_import_skill_dir_impl`）：
`TemporaryDirectory` → `copytree` 到 stage → `shutil.move` 到 `.<slug>.tmp-xxxx` → `rename` 到最终目录 →
`repo.create` 失败则 `rmtree` 回滚。本项目 `import_skill_dir` 完整复刻了这套"临时目录 + 原子 rename + 入库失败回滚"。

依赖展开：Yuxi 的 `expand_skill_closure`（skills.py:96-129）同样是"stack 判环 + seen 去重"的 DFS，
本项目 `expand_dependencies` 逻辑同源（并把菱形/真环的区分写进了注释与测试）。

## ④ 区别与取舍

在 [skills-system.md §7 的差异表](../../docs/openspec/skills-system.md) 基础上补"为什么"：

- **`read_skill` 专用工具 vs Yuxi 沙箱 `read_file` + 路径解析**：Yuxi 有每 thread 的容器沙箱文件系统，
  技能以 `/home/gem/skills/<slug>/SKILL.md` 存在，读文件即读技能，激活靠路径匹配。本项目没有沙箱，
  一个只读 `SkillService` 的专用工具就是最小且最稳的边界 —— 不用解析路径、不会读到技能之外的东西。
- **git clone vs npx skills CLI**：Yuxi 远程安装 shell 出 npx CLI 再抓屏解析输出。git clone 无外部 CLI
  依赖、退出码 / stderr 是稳定的结构化信号，解析不易碎；`--depth 1` 也省带宽。
- **内存 + 目录 vs PostgreSQL**：整个数据库层是二期。内置技能走内存缓存，用户 / 远程技能落盘为目录，
  `InMemorySkillRepository` 顶着 API。一份目录 + 一个内存表就近可 diff、可测，演示期完全够用，
  接了 PG 也只需换 Repository 实现，Service/Middleware 不动。
- **subprocess + 默认禁用 vs 容器沙箱**：容器沙箱是 Yuxi 多租户的刚需，本项目单机演示不值得这份复杂度。
  取而代之的是"无隔离但默认禁用远程技能 + 路径校验 + 超时 + 输出截断"，把风险面收敛到"人工审查后才启用"。
  这是一处**记账式**的取舍：明确不做隔离，用"默认禁用"兜住风险，需要多租户时再上沙箱。
