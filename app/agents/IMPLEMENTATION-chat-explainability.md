# 聊天可解释性（context_trace + context_hits）速读

> 背景规格见 [docs/openspec/chat-explainability.md](../../docs/openspec/chat-explainability.md)。
> 把"本轮命中了哪些知识、调了哪些工具"从黑盒变成 Chat 页折叠面板。设计文档：`docs/openspec/chat-explainability.md`

核心文件：

- `app/agents/context_trace.py` —— 记录器（双 ContextVar + 脱敏 + 请求级上限 + payload）
- `app/agents/middlewares.py` —— ToolRuntimeMiddleware 内的 start/finish 埋点
- `app/agents/tools/sql_context_tool.py` / `internal_docs_tool.py` / `graph_tool.py` —— 命中直录
- `app/services/chat_service.py` + `app/schemas/chat.py` —— `context_hits` SSE 事件与非流式字段
- `frontend/src/views/ChatView.vue` —— 折叠面板

## ① 功能与用法

Chat 页每条 AI 回答下方新增折叠面板（原生 `<details>`，默认收起）：

- **摘要行**："调用 3 个工具，命中 2 示例 / 1 术语 / 2 文档 / 1 图谱查询"——计数按
  `hit_key` 去重（同一示例被两次检索只算 1 个）；0 的分组不显示；触发上限时标"（已截断）"。
- **展开后按调用分组**：每次工具调用一行（工具名 + 状态徽标 + 耗时 + 脱敏参数摘要，
  鼠标悬浮看完整摘要），其下是该次调用命中的四类明细（术语/示例/文档片段/图谱查询摘要）。

SSE 流末事件顺序：`content* → sources → sql_result（条件）→ context_hits（条件）`。
无工具调用的纯闲聊消息不产生 `context_hits`，事件序列与改造前逐字节一致。

## ② 实现原理：双 ContextVar 与 call_id 的传递通道

照 `app/text2sql/feedback.py`（sql_result）同款 ContextVar 模式，但语义相反——
那边**取最后一次成功**（SQL 沉淀要结果），这边**追加保留全过程**（可解释性要过程：
"检索两次、第一次没命中"本身是信息）。同一种模式服务两种语义是刻意的对照。

- `_trace`（请求级）：ChatService 的 `chat/chat_stream` 进入 `use_context_trace()`。
- `_active_tool_trace`（调用级）：**call_id 从 middleware 到工具的传递通道**——
  `ToolRuntimeMiddleware.awrap_tool_call` 在 `record_tool_start` 后用
  `use_active_tool_trace(trace)` 包住 handler 执行；工具内的 `record_*_hits` 自行读取
  当前 active trace，**不显式接收 call_id**。sync 工具执行线程、async 并发、MCP
  override 都经 langchain 的 copy_context 正确归位（`ainvoke` 集成测试锁死）。
- middleware 是所有工具（门控本地 + MCP override）的**唯一必经点**——比解析
  `astream(stream_mode="messages")` 的 tool_call_chunks（分片到达聚合不可靠）可靠。

**安全边界（刻意设计）**：工具参数与异常不能原文下发浏览器。

- `summarize_args`：先脱敏再截断（160 字）。键名递归脱敏（password/token/api_key/
  secret/authorization/cookie/headers/credential → `"***"`）+ 字符串级模式脱敏
  （DSN 密码段、`Bearer xxx`）。
- 错误只传稳定枚举 `error_code`（timeout/tool_failure/circuit_open/unknown，按
  ToolRuntime status 映射）+ 本模块独立安全映射的中文 `public_message`；原始
  `str(exc)` 只留后端日志。**不改 tool_runtime.py**（`ToolExecutionResult` 无异常
  类型，fallback 文案拼接原文——都不能直接复用）。
- 请求级全局上限：`MAX_TOOL_CALLS=30`、`MAX_HITS_PER_TYPE=20` 按整个请求累计
  （非每调用各一份），触发置 `truncated`。

## ③ 参考项目是怎么做的，我们为何不同

- **Langfuse**（项目已可选接入）追踪模型/工具 span，但它是**运维侧**离线查看，
  面向开发者;本设计的面板是**用户侧**实时可见，面向 demo 与业务解释。两者互补
  而非替代——Langfuse 记原始异常（合理，运维要看），本面板只发脱敏摘要。
- **AIDE（生产级数据 Agent 调研）** 的"TUI + GUI 双层呈现"印证了这个方向：
  过程可解释是数据 Agent 的差异化能力，不是调试功能。

## ④ 取舍

- **score 刻意不记**：dense 路径带 Milvus 原生 score，但主链路混合检索 RRF 融合后
  丢弃，BM25/rerank/fake 口径不可比——展示语义不明的分数比不展示更误导。
- **args 只记脱敏后摘要**：SQL/日志查询/MCP 参数可能很长（SSE 流量放大 + 暴露面）；
  示例命中里的 SQL 反而全记——它本来就是注入模型的 few-shot，无额外暴露。
- **docs 的 hit_key** 用 `source:chunk_index`（chunk_index 缺失退化为 `source:rank`），
  与 `app/rag/document_utils.document_key` 同语义，保证重复检索可被摘要去重。
- **图谱只记查询摘要+规模**：复现只要 query + depth；全量边在工具结果里已有。
- **截断长度用模块常量不进 Settings**：展示细节而非部署开关（feedback.py 同款）。

## ⑤ 已知边界（README 同口径）

- analysis_agent 端点不接轨迹（无 recorder 空操作不出错）；Redis Streams 不带
  context_hits；不入会话历史（与 sources 一致，刷新即失）。**不是全平台统一可观测**。

## ⑥ 测试

`tests/test_context_trace.py`（17 例）：生命周期/嵌套键名与 DSN/Bearer 脱敏/截断顺序/
错误映射/去重摘要/请求级上限/四记录点直录/**真实 ainvoke 的 ContextVar 线程传播**/
同名工具并发归位/middleware 成功与降级轨迹（DSN 凭据不泄漏）/SSE 事件顺序与旧序列
不变/双路径序列化契约。全部 fake service 进程内完成，不依赖 Redis/Milvus/Docker。
