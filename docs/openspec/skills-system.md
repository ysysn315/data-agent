# Skills 系统 - OpenSpec（v2，已实现）

> v1 设计的差距分析见 [skills-optimization.md](skills-optimization.md)。
> 本文档描述当前已落地的实际设计。早期数量与成本数字保留为阶段测量，不代表未来新增技能后的精确值。

## 1. 模块定位

Skills 系统是 Data Agent 的插件化能力扩展机制，对齐 Yuxi 的三段式设计：
**渐进式披露 → 按需读取激活 → 工具门控**。

一个 skill 是**一个目录**（不是一个字符串）：

```
<skill-dir>/
├── SKILL.md          # YAML frontmatter + Markdown 正文（必须）
└── scripts/          # 随附可执行脚本（可选，对标 Yuxi mysql-reporter）
    └── query.py
```

## 2. 运行时三段式

```
① 披露    SkillsMiddleware.awrap_model_call
          system prompt 只注入每个技能的 名称 + 描述 + "先调用 read_skill(slug)"
          —— 注入成本与正文长度无关（早期 4 个内置技能阶段测量约 150 tokens）

② 激活    模型调用 read_skill(slug) → 返回 SKILL.md 全文
          middleware 在 awrap_tool_call 拦截调用结果
          → Command(update={"activated_skills": [slug]}) 写入 LangGraph state

③ 解锁    下一次模型调用时：
          - 该技能 dependencies.tools 声明的本地工具从隐藏名单移除（门控）
          - 该技能 dependencies.mcps 声明的 MCP server 工具懒加载并追加
```

关键实现约束（langchain v1）：

- 门控的本地工具必须**构建期注册**（挂在 `AgentMiddleware.tools` 上），
  否则 ToolNode 报 "not a valid tool"；请求期只是从 `request.tools` 过滤可见性
- 动态加载的 MCP 工具构建期不存在，必须在 `wrap_tool_call` 里
  `request.override(tool=实例)` 接管执行
- `ModelRequest` 不可变，所有修改走 `request.override(...)`

## 3. 文件结构

```
app/skills/
├── models.py            # SkillFrontmatter/SkillContent 解析、Skill（含 dir_path）、ExpandedSkills
├── repository.py        # Repository 契约与测试桩；生产依赖注入使用 SQLAlchemy Repository
├── service.py           # 加载/查询/依赖展开/目录导入（原子 rename + 回滚）
├── matching.py          # embedding 语义匹配 + jieba 回退
├── tools.py             # read_skill / run_skill_script（统一入口）
├── sandbox.py           # subprocess / Docker runner
├── middleware.py        # SkillsMiddleware（真实 langchain v1 AgentMiddleware）
├── remote_install.py    # git clone 整目录安装，默认 enabled=False
└── buildin/
    ├── schema-retrieval/
    ├── sql-generation/
    ├── data-visualization/     # dependencies.mcps: [chart-mcp]
    ├── sqlite-query/           # dependencies.tools: [execute_sql]，随附 scripts/query.py
    └── knowledge-graph/        # dependencies.tools: [graph_search]
```

## 4. 依赖模型

frontmatter 里三类依赖：

```yaml
dependencies:
  tools:  [execute_sql]      # 本地门控工具（构建期注册、激活后可见）
  mcps:   [chart-mcp]        # MCP server slug（激活后懒加载其工具）
  skills: [sql-generation]   # 其他技能（递归展开进披露列表）
```

展开规则（`expand_dependencies`）：
- 仅对 `skills` 边递归；tools/mcps 只取直接声明
- 环检测：分支内 stack 判环（告警跳过）；全局 seen 去重（菱形依赖不误报）
- 工具/MCP 解锁只看**已激活**技能的直接声明（`tools_of/mcps_of`）

## 5. 安全边界

- 脚本执行默认 subprocess（路径包含校验 + 30s 超时 + 输出截断）；
  **E 轮起可切 Docker 一次性容器**（`SKILL_SANDBOX_MODE=docker`：断网/只读/资源限额/超时防孤儿，
  详见 app/skills/IMPLEMENTATION-sandbox.md，含 macOS/colima 部署实测记录）
- 因此**远程安装的 skill 默认 enabled=False**，人工审查后经 API 启用
- execute_sql：SQLite URI `mode=ro` 引擎级只读 + SELECT/WITH 单语句校验（双保险）

## 6. API（/api/skills）

CRUD + enable/disable + remote/list + remote/install(-batch)，
依赖注入统一走 `app/core/dependencies.get_skill_service` 单例
（路由文件内不得自定义同名依赖 —— v1 曾因遮蔽导致列表恒空）。

## 7. 与 Yuxi 的差异（有意为之）

| 点 | Yuxi | 本项目 | 理由 |
|---|---|---|---|
| 读取入口 | 沙箱 read_file + 路径解析 | 专用 read_skill 工具 | 无沙箱文件系统，工具即边界 |
| 远程安装 | npx skills CLI + 屏幕抓取 | git clone 直下 | 无外部依赖、解析稳定 |
| 持久化 | PostgreSQL | SQLAlchemy async 元数据 + 目录正文 | 保持内容可检查，索引可事务化 |
| 语义匹配 | 无 | embedding + jieba 回退 | 外部 embedding 失败时仍可用 |
| 执行环境 | 每 thread 容器沙箱 | 默认 subprocess，可切一次性 Docker | 兼顾本地易用与脚本隔离 |

## 8. 测试

`tests/test_skill_service.py`（加载/链式/菱形/真环/中文匹配/目录导入/删除）、
`tests/test_skills_middleware.py`（假模型驱动真实 create_agent 的端到端：
披露不含正文、门控前后可见性、激活入 state、读不存在技能不激活）、
`tests/test_skill_tools.py`（read_skill / 脚本执行 / 路径穿越 / SELECT-only）、
`tests/test_skill_matching.py`（embedding 与 jieba 回退）、
`tests/test_script_sandbox.py`（runner 选择、容器参数、超时与环境降级）。
