# 聊天页可解释性 UI（chat-explainability）

> 状态：当前设计方案。分支 `feat/chat-explainability`（自 main 34d2579 切出，含 PR #30 的 sql_result 先例）。

## 1. 定位

Chat 页当前只有 SSE 文本流：命中了哪些 SQL 示例/术语/知识库文档、调用了哪些工具全黑盒——
项目三大卖点（示例库/RAG/图谱）在 demo 时"看不见"。本设计把检索命中明细 + 工具调用
轨迹随 SSE 下发（新事件 `context_hits`，旧前端忽略未知类型向后兼容），前端折叠面板展示。

与 `sql_result`（PR #30）同一套扩展模式：ContextVar 记录器 → 流末事件 → 前端分支。
关键差异：`sql_result` 取**最后一次成功**（用户要的是结果）；本设计**追加保留全过程**
（可解释性要的是过程——"检索两次、第一次没命中"本身就是信息）。同一种 ContextVar
模式服务两种语义，这是刻意对照。

## 2. 设计

### 2.1 记录器：双 ContextVar（app/agents/context_trace.py）

- `_trace`：请求级 `ContextTrace`，`use_context_trace()` 由 ChatService 进入；
- `_active_tool_trace`：**当前执行中的 ToolCallTrace**——middleware `record_tool_start`
  后用 `use_active_tool_trace(trace)` contextmanager 包住 `handler(request)` 执行；
  工具内 `record_*_hits(...)` 自行读取，**不显式接收 call_id**。这是 call_id 从
  middleware 到工具的传递通道：sync 工具执行线程、async 并发、MCP override 都经
  langchain 的 copy_context 正确归位，同名工具并发不串。

```
ToolCallTrace: call_id(request.tool_call["id"]) / name / args(脱敏截断摘要)
               / duration_ms / status / attempts / error_code / public_message
               / hits(该次调用的命中明细，嵌套非全局)
Hits: terms / examples / docs / graph 四类，每条带 rank(调用内名次) + hit_key
```

hit_key：terms=term、examples=question、**docs 复用 `app/rag/document_utils.document_key()`
语义（优先 source+chunk_index，缺失时内容摘要哈希，不拼完整 chunk）**、graph=kind:query。

### 2.2 脱敏：先脱敏后截断，错误只传稳定枚举

工具参数与异常不能原文下发浏览器（可能含数据库地址、文件路径、凭据）：

- `summarize_args()`：①键名递归脱敏——`password/token/api_key/secret/authorization/
  cookie/headers/credential`（含嵌套 dict/list）值替换 `"***"`；②字符串级模式脱敏——
  DSN 密码段（`scheme://user:pass@host`）、`Bearer xxx`、`Authorization: xxx`；
  ③之后才截断到 160 字。未知 MCP 工具默认只展示参数名列表。
- 错误：`ToolExecutionResult` 只有原始 error 字符串（异常类名捕获后已丢失，fallback
  文案拼接原文）——**不改 tool_runtime.py**，响应只传稳定枚举 `error_code`
  （tool_failure / circuit_open / cancelled / unknown，对齐 ToolRuntime 实际终态 degraded/circuit_open 与 middleware 捕获的取消）+ 本模块独立
  安全映射的中文 `public_message`。原始异常只留后端日志/Langfuse。

### 2.3 上限：请求级全局

`MAX_TOOL_CALLS=30`、`MAX_HITS_PER_TYPE=20` 按**整个请求累计**（非每调用各一份，
否则重复调用仍放大 SSE）；任一触发置外层 `truncated=true`，前端展示"已截断"。

### 2.4 四处记录点（一行式直录，无 recorder 空操作）

| 文件 | 记什么 |
|---|---|
| `sql_context_tool.py` | term/example 明细 |
| `internal_docs_tool.py` | 保留 record_sources（sources 事件字节不变），加 doc 明细 |
| `graph_tool.py` | 命中+未命中分支的 query/summary/count（不记全量边） |
| `middlewares.py` awrap_tool_call | 所有工具（含门控/MCP override）唯一必经点；记模型原始调用的 name/args |

middleware 埋点而非流解析：`astream(stream_mode="messages")` 只透传 AIMessage，
tool_call_chunks 分片聚合不可靠；middleware 两行换全量轨迹。

### 2.5 下发与前端

- ChatService 流末顺序 sources → sql_result（条件）→ **context_hits（非空才发）**；
  非流式 dict 加键；schema 新增各 Payload（无原始 error/args 全量）。
- ChatView 原生 `<details>` 默认折叠：摘要行"调用 3 个工具，命中 2 示例 / 1 术语 /
  2 文档 / 1 图谱查询"——**按 hit_key 去重计数**（详情保留每次调用，摘要不重复计）；
  展开按调用分组（工具行 name+状态徽标+耗时+args title 悬浮 + 该次命中明细）。

## 3. 刻意取舍

- **score 不展示（删除而非"有则带"）**：dense 路径带 Milvus 原生 score，但主链路混合
  检索 RRF 融合后丢弃，BM25/rerank/fake 口径不可比——展示语义不明的分数比不展示更误导。
- **args 只记脱敏后 160 字摘要**：SQL/日志查询/MCP 参数可能很长（SSE 流量放大 +
  暴露面扩散）；示例命中里的 SQL 反而全记——它本来就是注入模型的 few-shot，无额外暴露。
- **图谱只记查询摘要+规模**：复现只要 query+depth；全量边在工具结果里已有。
- **截断长度用模块常量不进 Settings**：展示细节而非部署开关（feedback.py 同款零 Settings 依赖）。

## 4. 已知边界（README 同口径，不写"全平台统一可观测"）

- analysis_agent 端点不接轨迹（空操作不出错）；Redis Streams 不带 context_hits；
  不入会话历史（与 sources 一致，刷新即失）。

## 5. 文件结构

```
新增：app/agents/context_trace.py、tests/test_context_trace.py
修改：sql_context_tool.py、internal_docs_tool.py、graph_tool.py、middlewares.py、
      chat_service.py、routes_chat.py、schemas/chat.py、ChatView.vue、README.md
```

## 6. 测试与验收

测试（全部 fake service 进程内，不因无 Redis/Milvus/Docker 而 skip）：脱敏（键名嵌套+
DSN/Bearer/Authorization 字符串级+截断顺序）、错误脱敏、去重计数、并发 call_id、
**`await tool.ainvoke(...)` 真实 LangChain 线程传播**（run_in_executor + copy_context，
非裸 asyncio.to_thread）、MCP override 链只记一次、序列化契约、请求级上限。

手工三组场景：SQL 命中（复购率问题）/ RAG 命中（ENABLE_KB_TOOL+Milvus+文档）/
图谱命中（邻居+路径查询）。

验收后交 Codex 人工复审，修复后提 PR，不自动合并。
