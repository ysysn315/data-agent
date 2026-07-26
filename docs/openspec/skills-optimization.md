# Skills 系统 - 对标 Yuxi 差距分析与优化方案

> 分析日期：2026-07-26　分支：`feat/skills-system`
> **落地记录（2026-07-26，分支 feat/skills-v2-mcp）**：P0 与 P1 已全部实现并通过
> 22 个 pytest 用例（含 §3.1-§3.5 全部缺陷修复、目录模型、渐进披露、激活门控、
> 远程安装整目录+默认禁用、sqlite-query 脚本技能）；P2 中 13（环检测）已完成，
> 10/11/12/14 仍为二期。v2 实际设计见 skills-system.md。安全提醒已按建议采纳
> （远程 skill 默认 enabled=False）。
> 对照实现：`/Users/ysn/projects/yuxi-reference/backend/package/yuxi/agents/skills/` 及 `agents/middlewares/skills.py`

## 1. 核心结论

两套系统在**一个根本问题**上的答案不同，其余差距几乎都从这里派生：

> **Skill 是什么？**
> Yuxi：**一个目录**。数据库只存元数据（`slug/name/description/deps/dir_path`），SKILL.md 和随附文件躺在文件系统上。
> 本项目：**一个字符串**。`Skill.content` 存整个 SKILL.md，没有 `dir_path`，随附文件在安装时被丢弃。

由此派生出三个能力差距：

| | Yuxi | 本项目 |
|---|---|---|
| Skill 能否携带可执行脚本 | 能（`mysql-reporter/scripts/*.py`） | **不能** |
| 提示词注入量 | 每个 skill 两行（名称+描述+路径） | **全文正文，含全部传递依赖** |
| 工具何时可见 | 模型读了 SKILL.md 才解锁 | 无门控概念 |

Yuxi 的旗舰内置 skill `mysql-reporter` 的工作方式，正好是本项目最该复用的模式：SKILL.md 里写 `cd /home/gem/skills/mysql-reporter && uv run scripts/query.py --sql "..."`。**对一个数据分析 Agent 而言，"skill 能带着 SQL 脚本/图表模板一起分发"是这个模式最大的价值，而这恰恰是当前实现拿不到的部分。**

## 2. 逐项对照

图例：🔴 影响可用性　🟡 影响可维护性/成本　🟢 已对齐或有意简化

### 2.1 存储与内容模型

| 维度 | Yuxi | 本项目 | |
|---|---|---|---|
| 内容存储 | 文件系统 `<save_dir>/skills/<slug>/`，DB 只存索引 | `Skill.content` 字符串 | 🔴 |
| 随附文件 | 整个目录树，`copytree` + 原子 rename 导入 | 只读 `SKILL.md`，其余丢弃 | 🔴 |
| 文件管理 API | 文件树浏览 + 按扩展名白名单增删改查 | 无 | 🟡 |
| ZIP 上传/导出 | 支持，校验路径穿越（`..`/绝对路径） | 无 | 🟡 |
| 版本 | `version` + `content_hash`（sha256 目录哈希） | frontmatter 里有 `version` 字段但从不使用 | 🟡 |
| slug 冲突 | 自动改名 `-v2/-v3` 并回写 frontmatter | 直接报错 | 🟢 有意简化 |

### 2.2 注入机制（差距最大）

| 维度 | Yuxi | 本项目 | |
|---|---|---|---|
| 渐进式披露 | **只注入 `- **{name}**: {desc}` + `-> Read <path>`**，正文由模型按需 `read_file` | **注入全部正文**（`models.py:172`） | 🔴 |
| 激活机制 | 拦截 `read_file` 结果，路径匹配 `/home/gem/skills/<slug>/SKILL.md` 即写入 state `activated_skills` | 无 | 🔴 |
| 工具门控 | 构建期全量注册（保证 ToolNode 可执行），请求期从 `request.tools` 过滤掉未激活 skill 的工具 | `request.context["required_tools"] = [名字字符串]`，**从未被消费** | 🔴 |
| 中间件 API | `awrap_model_call(request, handler)`，`request.override(...)`（不可变 dataclass） | `modify_model_request(request)` + 直接赋值 —— **不是 LangChain v1 的真实接口** | 🔴 |

注入成本实测（当前 3 个内置 skill）：

```
data-visualization  741 chars
schema-retrieval    777 chars
sql-generation      848 chars
TOTAL              2366 chars  ≈ 1183 tokens / 每次请求
外推到 20 个 skill：≈ 7900 tokens / 每次请求，且无法关闭
```

Yuxi 同样 20 个 skill 的注入量约为 20 × 2 行 ≈ 300 tokens，**且与正文长度无关**。

### 2.3 依赖

| 维度 | Yuxi | 本项目 | |
|---|---|---|---|
| 三类依赖 | tools / mcps / skills | 同 | 🟢 |
| 递归展开 | 仅对 `skills` 递归；tools/mcps 只取直接激活的 skill | 仅对 `skills` 递归 | 🟢 |
| 环检测 | 每分支独立 `stack` 副本，真环才告警 | **全局 `visited` 集合**，菱形依赖误报（见 §3.2） | 🟡 |
| 深度限制 | 无（靠环检测） | `max_depth=3` 静默截断 | 🟡 |
| 写入期校验 | tool 必须在注册表中、mcp 必须已启用、skill 依赖必须可访问、拒绝自引用 | **完全没有校验** | 🟡 |

### 2.4 持久化与权限

| 维度 | Yuxi | 本项目 | |
|---|---|---|---|
| 存储 | PostgreSQL + async SQLAlchemy 2.0 | 内存 dict，**重启即丢** | 🟡 |
| Repository 抽象 | `SkillRepository(db_session)` | `SkillRepository = InMemorySkillRepository`（别名，**没有接口/ABC**） | 🟡 |
| 并发 | `SELECT ... FOR UPDATE`、FS/DB 双写补偿回滚 | 无 | 🟢 单用户可接受 |
| 共享权限 | `share_config: {access_level: global/department/user}` + `user_can_access_skill` / `user_can_manage_skill` | 字段存在但**未实现**（`repository.py:583` TODO）；远程安装把它挪作他用存 `{"source": ...}` | 🟢 §10 已列为二期 |
| 依赖越权防护 | `can_skill_depend_on` 子集包含规则 | 无 | 🟢 二期 |

### 2.5 远程安装

| 维度 | Yuxi | 本项目 | |
|---|---|---|---|
| 下载方式 | `npx -y skills` CLI（隔离 `$HOME`） | **`git clone --depth 1`** | 🟢 **本项目更优**：无外部 CLI 依赖、无每次联网装包 |
| 输出解析 | 屏幕抓取 CLI 输出（脆弱） | 直接遍历文件系统 | 🟢 **本项目更优** |
| 查找路径 | `.agents/skills/<name>` | `skills/` → `<name>/` → `.agents/skills/` 三级 | 🟢 |
| 批量优化 | 一次 clone 多个 skill | 同 | 🟢 |
| 安装内容 | **整个目录** | **只有 SKILL.md** | 🔴 |
| 路径穿越校验 | `_validate_zip_paths` + `_resolve_relative_path` | 不适用（不落盘） | — |

### 2.6 运行时执行环境

| 维度 | Yuxi | 本项目 | |
|---|---|---|---|
| 脚本执行 | 每 thread 一个 Docker/K8s 沙箱，`/home/gem/skills` 只读 bind mount | 无 | 🟢 §9 已明确"沙箱太重，先用 subprocess" |
| 文件后端 | `SelectedSkillsReadonlyBackend` 按 slug 白名单过滤 | 无 | — |

**这一项不建议照抄。** Yuxi 需要容器是因为它是多租户的；本项目 §9 已经决定用 subprocess 替代，照抄 provisioner 架构会把整个项目拖垮。

### 2.7 语义匹配（本项目独有，但目前失效）

Yuxi **没有**语义匹配 —— 它靠渐进式披露让模型自己决定读哪个。本项目的 `match_skills_by_query` 是一处主动创新，方向值得保留，但当前实现对中文完全无效（见 §3.1）。

### 2.8 测试

| Yuxi | 本项目 |
|---|---|
| 8 个测试文件（router / backend / middleware / service / remote_install / 错误处理），跑在 `backend/test/unit/` | `test_skills.py` 单个 print 脚本，**无断言**；`tests/` 目录为空 |

## 3. 已验证的缺陷

### 3.1 🔴 `match_skills_by_query` 对中文查询全部失效

`service.py:413-416` 用 `str.split()` 做分词 —— 中文没有空格，整句会被切成**一个 token**，与描述的交集恒为空。三处打分规则实测：

```
'查询数据库表结构'          -> []                        # 期望 schema-retrieval
'制作图表'                 -> []                        # 期望 data-visualization
'帮我看下上个月各州的销售额'  -> []                        # 期望 sql-generation
'生成 SQL 查询'            -> [('sql-generation', 1)]   # 仅因 "sql" 恰好被空格分开
```

`skill.slug in query_lower`（要求用户原话里出现 "schema-retrieval"）和 `skill.name.lower() in query_lower`（name 是英文）在中文场景同样恒不命中。

**这个缺陷被测试脚本掩盖了**：`test_skills.py` 只 `print` 返回值，返回空列表时该 query 下什么都不打印，看起来和"通过"没有区别。所以"测试通过：语义匹配工作"这个结论并不成立 —— `auto_match=True` 模式目前等价于不挂载任何 skill。

### 3.2 🟡 菱形依赖被误报为循环依赖

`service.py:343` 用全局 `visited` 集合判环。对 A→B、A→C、B→D、C→D：

```
expanded order:   ['a', 'b', 'c', 'd']        # 结果正确
warnings emitted: ['检测到循环依赖，跳过: d']    # 告警错误
```

结果无误（d 只入一次），但日志会在正常的共享依赖上持续报假环。Yuxi 的做法是分支内 `stack` 副本判环 + 全局 `seen` 去重，两者分开。

当前三个内置 skill 恰好是链式（`data-visualization → sql-generation → schema-retrieval`），所以没触发。

### 3.3 🔴 `routes_skills.py` 的 `get_skill_service()` 覆盖了真正的单例

`routes_skills.py:80-83` 自己定义了一个返回裸 `SkillService()` 的依赖函数，遮蔽了 `core/dependencies.py:get_skill_service()` 里那个会加载内置 skill 的单例。路由用的是前者，**所以 `GET /api/skills` 返回空列表**。

### 3.4 🔴 中间件不是 LangChain v1 的真实接口

不只是"注释掉的 import 需要恢复"。真实接口是 `awrap_model_call(self, request, handler) -> ModelResponse`，且 `ModelRequest` 是**不可变 dataclass**，必须用 `request.override(...)`。当前的 `modify_model_request` + `request.system_message = ...` 直接赋值，恢复 import 后会直接失败。这部分需要重写而非解注释。

### 3.5 🟡 工具依赖链路从未被验证

三个内置 skill 只声明了 `skills:` 和 `mcps:`，**没有一个声明 `tools:`**。`ExpandedSkills.tools` 在测试中恒为空列表，`SkillsToolFilter` 从未被调用过。

## 4. 优化方案

排序原则：先让链路真的跑通（P0），再改架构（P1），再谈持久化（P2）。§10 已划为二期的（多租户权限、容器沙箱）不在本方案内。

### P0 · 让 Skills 真正接入 Agent

目标：`ChatAgent` 挂上 skills 后，模型能看到 skill 列表并按需读取。这是当前唯一"看起来完成了但实际没有"的部分。

1. **重写 `middleware.py` 对齐真实 API**
   - 恢复 `from langchain.agents.middleware import AgentMiddleware, ModelRequest`
   - `modify_model_request` → `awrap_model_call(self, request, handler)`，用 `request.override(system_message=..., tools=...)`
   - 删掉三个假的占位类
2. **修 `routes_skills.py` 的依赖注入**：删掉局部 `get_skill_service`，改用 `core/dependencies` 的单例（§3.3）
3. **修中文分词**：用 `jieba`（`my-agent-original/requirements.txt` 里已有）替换 `str.split()`，或直接改用项目已有的 embedding 能力（见 P2-3）
4. **给 `test_skills.py` 加断言并迁到 `tests/`**：当前脚本无法区分"匹配到 0 个"和"匹配正确"

**验收**：`GET /api/skills` 返回 3 条；`ChatAgent` 带 skills 跑一轮真实对话，日志里能看到注入的 system prompt。

### P1 · Skill 从"字符串"改成"目录"

这是整个方案的核心，也是解锁后续所有能力的前提。

5. **改存储模型**
   - `Skill` 增加 `dir_path`，`content` 降级为"根 SKILL.md 的缓存"（或直接去掉，按需从磁盘读）
   - 内置 skill 目录不变，用户/远程 skill 落到 `<save_dir>/skills/<slug>/`
   - 导入用 `copytree` → 临时目录 → 原子 `rename`，失败回滚（照抄 Yuxi `service.py:680-717`，这段值得逐行参考）
6. **远程安装改为复制整个目录**（`remote_install.py` 现在只读 SKILL.md）
   - 顺带补路径穿越校验：拒绝 `..` 和绝对路径
7. **改成渐进式披露**
   - `ExpandedSkills.build_system_prompt()` 只输出 `- **{name}**: {description}` + 读取路径
   - 加一个 `read_skill(slug)` 工具作为读取入口 —— **本项目没有沙箱文件系统，这个工具就是 Yuxi `read_file` + 只读 backend 的轻量替代**，且天然只能读 skills 目录，不需要额外的路径白名单
8. **加激活门控**
   - `AgentState` 增加 `activated_skills`（带去重 reducer）
   - `read_skill` 调用成功后把 slug 并入 state
   - 未激活 skill 的 `tools` 从 `request.tools` 过滤掉，但**仍要在建图时注册**，否则 ToolNode 会报 "not a valid tool"（Yuxi 在 `skills.py:151-154` 专门注释了这个坑）
9. **补一个带脚本的内置 skill 验证闭环** —— 建议直接做 `sql-generation` 的脚本化版本，对标 `mysql-reporter`：`scripts/query.py` 连 SQLite 跑 Kaggle 电商库。这一步做完，"Skills 插件化"才算真的有演示价值。

**验收**：新增一个带 `scripts/` 的 skill，模型先读 SKILL.md、再调用其中声明的工具执行脚本，全过程可在日志复现。

### P2 · 持久化与健壮性

10. **落地真实 Repository**：抽出 ABC，内存实现留作测试桩，加 SQLite/PG 实现。注意本项目**目前完全没有 DB 层**，这一步实际是在给整个项目引入持久化基础设施，成本被低估了 —— 建议单独排期。
11. **写入期依赖校验**：tool 在注册表中、mcp 已启用、skill 依赖存在、拒绝自引用（参考 Yuxi `service.py:440-474`）
12. **语义匹配升级**：项目已经有 `app/rag/embeddings.py` + BGE + Milvus，把 skill 的 `description` 向量化做召回，比关键词匹配合理得多，**也是相对 Yuxi 的真实增量**（Yuxi 没有这一层）
13. **修环检测**：分支 `stack` 判环 + 全局 `seen` 去重分离（§3.2）
14. **补 pytest 用例**：可直接对照 Yuxi `backend/test/unit/middlewares/test_skills_middleware.py`，尤其是"门控工具激活前不可见、激活后可见"那组断言

### 明确不做（对齐 REQUIREMENTS §9/§10）

- 容器沙箱 / provisioner —— 用 subprocess + 超时替代
- 多租户 `share_config` / 部门权限 / `can_skill_depend_on`
- 文件树管理 API、ZIP 导入导出
- skill 版本历史与 `content_hash` 升级检测

### 一处安全提醒

P1-9 之后，远程安装的 skill 会携带可执行脚本，而 `remote_install.py` 对 `owner/repo` 不做任何白名单。Yuxi 靠只读 bind mount + 容器隔离兜底，本项目用 subprocess 则**没有任何隔离**。在引入脚本执行的同时，至少需要：远程 skill 默认 `enabled=False` 需人工确认，或脚本执行限定在内置/本地上传的 skill。这一点应在动手前决定，不要留到之后补。

## 5. 值得直接精读的 Yuxi 源码

| 文件 | 看什么 |
|---|---|
| `agents/middlewares/skills.py:219-272` | `awrap_model_call` 的完整注入逻辑 |
| `agents/middlewares/skills.py:391-461` | 读取 SKILL.md → 激活 skill 的拦截实现 |
| `agents/middlewares/skills.py:149-164` | 为什么门控工具仍需在建图时注册 |
| `agents/skills/service.py:680-717` | 目录导入的原子性与回滚 |
| `agents/skills/service.py:440-474` | 依赖写入期校验 |
| `agents/skills/buildin/mysql-reporter/` | 带脚本的 skill 长什么样（本项目 P1-9 的模板） |
