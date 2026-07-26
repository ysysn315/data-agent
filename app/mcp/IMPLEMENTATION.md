# MCP 系统速读（注册表 → 工具加载 → 技能懒加载）

> 设计规格见 [docs/openspec/mcp-system.md](../../docs/openspec/mcp-system.md)（本文不重复，
> 只讲"全链路怎么串 + 相对 Yuxi 修了哪四个问题"）。分支来源：feat/skills-v2-mcp。
> 定位：把任意 MCP server（图表渲染、数据库连接器等）注册进平台，其工具经 langchain-mcp-adapters
> 转成 LangChain tools 供 Agent 调用；与 Skills 联动，激活技能后才懒加载对应 server 的工具。

核心文件：

- `models.py` —— `MCPServer`（pydantic）+ `to_client_config()` 按 transport 投影连接配置
- `service.py` —— JSON 注册表 CRUD + `load_tools`（并行 / 超时 / 缓存 / 失败隔离）+ `test_server`

## ① 功能与用法：一条链路

```
MCPServer（注册表 JSON）
  └─ to_client_config()               # 按 transport 投影出连接配置
       └─ MultiServerMCPClient(cfg)   # langchain-mcp-adapters 建连
            └─ get_tools()            # 拉取该 server 的工具（LangChain BaseTool）
                 └─ SkillsMiddleware  # 技能激活后 load_tools(mcps) 懒加载，追加进 request.tools
                      └─ wrap_tool_call(request.override(tool=实例))  # 接管动态工具执行
```

典型用法（`data-visualization` 技能声明 `dependencies.mcps: [chart-mcp]`）：注册

```json
{"slug":"chart-mcp","transport":"stdio","command":"npx","args":["-y","@antv/mcp-server-chart"]}
```

后，模型 `read_skill("data-visualization")` 激活该技能，`SkillsMiddleware` 即调
`mcp_service.load_tools(["chart-mcp"])` 把图表渲染工具懒加载进本次调用。未激活则一次连接都不发起。

API（`/api/mcp/servers`）：GET 列表 / GET 详情 / POST 注册 / PUT 更新 / DELETE / POST enable|disable / POST test。

## ② 实现原理与关键技术

### to_client_config：按 transport 门控字段投影

`MCPServer` 一个模型装两类 transport 的字段，`to_client_config()` 按类型只投影相关字段：
`stdio` 出 `command/args/env`；`sse/streamable_http` 出 `url/headers/timeout/sse_read_timeout`。
**注意 `disabled_tools` 不进 `to_client_config`** —— 它只影响返回值过滤，不是连接参数，
所以配置哈希对它稳定（见下文缓存）。model_validator 兜底：http 类必须有 http(s) url，stdio 必须有 command。

### 注册表持久化

JSON 文件 `save_dir/mcp_servers.json`，`_save_registry` 走 tmp + rename 原子写；
`_load_registry` 解析失败**显式 raise 不静默清空**（配置损坏要暴露，不能悄悄丢服务器）。

### load_tools：并行 + 超时 + 缓存 + 失败隔离

- **并行**：`asyncio.gather(*(_load_one(s) ...))`，跨 server 独立。
- **失败隔离**：`_load_one` 任何异常 / 超时都 `return []`，单个坏 server 不拖垮整批。
- **超时**：`_fetch_tools` 用 `asyncio.wait_for(client.get_tools(), timeout=load_timeout)`（默认 20s）
  包住建连拉工具 —— 这是相对 Yuxi 的关键修正（见 ③）。
- **缓存**：键 = `f"{slug}:{config_hash}"`，`config_hash` 是 `to_client_config()` 的 sha256 前 16 位。
  配置一变哈希就变、缓存自然失效；CRUD / 启停显式 `_invalidate(slug)` 清该 slug 全部缓存键。
  缓存里存**全量未过滤**工具，`disabled_tools` 只在返回时过滤，不污染缓存。

### test_server：启用前就能验证

`test_server` 直接 `_fetch_tools`，**不要求 `enabled`、不写缓存**，返回工具名 + 截断描述，
让运维在"注册 → 启用"之间先验证 server 连得通、工具对不对，再决定启用。

### 与 Agent 联动（懒加载单路径）

主路径就是 Skills：`SkillsMiddleware` 在技能激活后 `load_tools(激活技能的 mcps)`，把动态工具追加进
本次 `request.tools`，并登记到 middleware 的 `_mcp_tools`。因为这些工具**构建期不存在**、没进 ToolNode，
执行时必须在 `wrap_tool_call` 里 `request.override(tool=实例)` 接管，否则同样会被 langchain v1
判为 "not a valid tool"。

## ③ Yuxi 是怎么做的 + 修正的四个问题（对照 yuxi-reference）

`yuxi-reference/backend/package/yuxi/agents/mcp/service.py` 的 `get_mcp_tools`（197 行起）架构与本项目同源：
连接拉全量 → 全量入 `_mcp_tools_cache`（键 `server_slug:config_hash`）→ 返回时按 `disabled_tools` 过滤。
`mcp_router.py` 提供 CRUD / test / 启停 / per-tool toggle 端点。本项目在此基础上修了四个问题：

1. **stdio 无超时（会卡死请求）**：Yuxi 的 `get_mcp_tools` 里 `await client.get_tools()` **没有超时包裹**
   （service.py:248）。stdio transport 会拉起子进程，子进程挂起就把整个请求卡死。本项目用
   `asyncio.wait_for(..., timeout=load_timeout)` 包住，超时即 `return []` 降级。
2. **disabled server 不可测**：Yuxi 的测试 / 获取都走 `get_enabled_mcp_server_config`（`enabled == 1` 过滤，
   service.py:166、221），启用前拿不到配置 → 没法在启用前验证一个 server 连不连得通。本项目 `test_server`
   不要求 `enabled`，注册后立刻可测。
3. **per-tool toggle 全量重连**：Yuxi 的 `toggle_tool_enabled`（service.py:503-542）只是改 `disabled_tools`，
   却调 `clear_mcp_server_tools_cache` 把该 server 的**整份工具缓存清掉**，下次取工具被迫重连 + 重列全部工具，
   只为隐藏一个工具。本项目把 `disabled_tools` 排除在 `to_client_config` 之外 → 它不进 `config_hash` →
   连接缓存对"启停单个工具"这类可见性变更保持稳定，过滤只作用于返回值，不需要重连。
4. **`enabled` 用 Integer**：Yuxi 的 `MCPServer.enabled` 是 Integer 1/0（`set_server_enabled` 里
   `server.enabled = 1 if enabled else 0`，service.py:491），到处 `bool(...)` 转换、易错。本项目直接用 `bool`。

## ④ 区别与取舍

- **JSON 注册表 vs PostgreSQL**：Yuxi 用 PG 表存 server 配置并做多用户/内置同步（`ensure_builtin_mcp_servers_in_db`）。
  本项目与 Skills 一致，数据库层整体二期，先用原子写的 JSON 文件顶上；接口不变，二期换存储不动 Service。
- **懒加载单路径 vs Yuxi 双路径**：Yuxi 既有 `get_tools_from_all_servers`（构建期全量注册）又有技能懒加载。
  本项目只做**技能懒加载这一条主路径**（不激活不连接，最省资源、最贴合"按需披露"），
  "Agent 构建期直接全量配置"的第二条路径按需二期补。
- **安全边界（stdio = 命令执行面）**：`transport=stdio` 的 `command/args` 可执行任意命令，MCP 配置面
  本质就是服务器命令执行面。当前 `get_current_user` 是占位鉴权（见 dependencies.py），
  **生产开放该 API 前必须先落地真实鉴权**；多用户场景还需补 http url 主机白名单、stdio 命令白名单或整体禁用。
  Yuxi 同样无 SSRF / 命令白名单，靠 admin-only 兜底 —— 这条边界两边都要靠部署侧闸门，代码层不自作主张放开。
