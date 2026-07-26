# MCP 系统 - OpenSpec（已实现）

> 参考 Yuxi `agents/mcp/service.py` + `mcp_router.py` 简化实现，
> 并修正其分析中发现的已知问题。分支 feat/skills-v2-mcp。

## 1. 模块定位

标准化外部工具接入：把任意 MCP server（图表渲染、数据库连接器等）注册进平台，
其工具经 langchain-mcp-adapters 转成 LangChain tools 供 Agent 调用。
与 Skills 联动：技能 frontmatter 声明 `dependencies.mcps`，激活后懒加载。

## 2. 数据模型（app/mcp/models.py）

`MCPServer`（pydantic）：slug / name / description / transport(stdio|sse|streamable_http) /
url+headers+timeout+sse_read_timeout（http 类）/ command+args+env(stdio) /
enabled / disabled_tools。

- 校验：http 类必须有 http(s) url；stdio 必须有 command；slug 格式同 skills
- `to_client_config()` 按 transport 投影 MultiServerMCPClient 连接配置
  （字段按类型门控，对齐 Yuxi to_mcp_config）
- enabled 用 bool（Yuxi 用 Integer 1/0，易错，不抄）

## 3. 服务层（app/mcp/service.py）

- **注册表**：JSON 文件（`save_dir/mcp_servers.json`，原子写 tmp+rename），
  与 Skills 一致暂不引入数据库；损坏时显式报错不静默清空
- **工具加载** `load_tools(slugs)`：
  - 并行 gather，单 server 失败/超时隔离（返回 []，不拖垮整体）
  - `asyncio.wait_for` 包裹 get_tools() —— 修正 Yuxi stdio 无超时、子进程挂起卡死请求的问题
  - 缓存键 = `slug:配置sha256前16位`，配置变更自动失效；CRUD/启停显式失效
  - `disabled_tools` 只过滤返回值，不污染缓存
- **test_server**：不要求 enabled（修正 Yuxi 无法在启用前验证 server 的问题）、不写缓存

## 4. API（/api/mcp/servers）

GET 列表 / GET 详情 / POST 注册 / PUT 更新 / DELETE / POST enable|disable / POST test。

## 5. 与 Agent 的联动

两条路径（对齐 Yuxi 双路径，简化）：
1. **技能懒加载**（主路径）：SkillsMiddleware 在技能激活后
   `mcp_service.load_tools(激活技能声明的 mcps)`，追加进本次 `request.tools`；
   动态工具在 `wrap_tool_call` 里 `request.override(tool=实例)` 接管执行
2. 直接配置路径（Agent 构建期全量注册）：二期按需补充

示例：`data-visualization` 技能声明 `mcps: [chart-mcp]`，
注册 `{"slug":"chart-mcp","transport":"stdio","command":"npx","args":["-y","@antv/mcp-server-chart"]}`
后，激活该技能即可获得图表渲染工具。

## 6. 安全边界（重要）

MCP 配置面 = 服务器命令执行面（stdio transport 可执行任意命令）：
- 当前项目无真实鉴权（get_current_user 为占位），**生产必须先落地鉴权再开放该 API**
- Yuxi 同样无 URL/SSRF 校验、无 stdio 命令白名单（admin-only 是它唯一的闸），
  本项目二期若做多用户，需增加：http url 主机白名单、stdio 命令白名单或整体禁用

## 7. 测试

`tests/test_mcp_service.py`：transport 校验 / 配置投影字段门控 / CRUD 持久化 /
禁用与缺失隔离 / **真实 FastMCP stdio server 端到端**（注册 → load_tools →
ainvoke 调用 → disabled_tools 过滤）。
