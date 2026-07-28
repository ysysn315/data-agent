# Agent 核心速读（chat_agent v2 + LLMFactory + ToolRuntimeMiddleware）

> 定位：这是"一句中文问题 → 工具编排 → 回答"的中枢。相对 my-agent（手写 StateGraph + 绑死 ChatTongyi），
> v2 做了两件事：**用 langchain v1 `create_agent` + 中间件栈替换手写图**，**用 `LLMFactory` 解除厂商绑定**；
> 并把 my-agent 的工具熔断能力保留成一个中间件（`ToolRuntimeMiddleware`）挂回执行链。
> Skills / MCP 联动见 [app/skills/IMPLEMENTATION.md](../skills/IMPLEMENTATION.md) 与
> [app/mcp/IMPLEMENTATION.md](../mcp/IMPLEMENTATION.md)。

核心文件：

- `chat_agent.py` —— `ChatAgent`，薄封装 `create_agent`，提供 `chat` / `chat_stream`
- `../core/llm.py` —— `LLMFactory`，统一造 LLM（任意 OpenAI 兼容接口）
- `middlewares.py` —— `ToolRuntimeMiddleware`（把熔断/重试/降级接进工具执行链）
- `tool_runtime.py` —— `safe_tool_execute` + 熔断状态机 + 每工具策略（从 my-agent 原样保留）
- 组装点：`../core/dependencies.py` 的 `get_chat_agent`

## ① 功能与用法

`get_chat_agent`（`dependencies.py`）是唯一组装点，单例：

```python
_chat_agent = ChatAgent(
    llm=LLMFactory.create_llm(),  # 任意 OpenAI 兼容接口，不绑厂商
    tools=base_tools,  # get_current_datetime + read_skill/run_skill_script + 可选 tavily/kb
    middleware=[
        SkillsMiddleware(skill_service, mcp_service, gated_tools=[execute_sql, schema_search]),
        ToolRuntimeMiddleware(),  # 熔断/重试/降级，顺序在 Skills 之后
    ],
)
```

`ChatAgent` 只做三件事：`create_agent` 建图、`_build_messages` 拼消息（summary + history + question）、
`chat`（`ainvoke` 取末条 content）/ `chat_stream`（`astream(stream_mode="messages")` 逐 token 产出，
兼容 content 为 str 或分段 list）。门控工具作为 `gated_tools` 交给 `SkillsMiddleware`，不直接进 `tools`。

## ② 实现原理与关键技术

### create_agent + 中间件栈的组装

`ChatAgent.__init__` 直接 `create_agent(model, tools, system_prompt, middleware)`。能力全部以
`AgentMiddleware` 挂载而非写进图节点：`SkillsMiddleware` 负责披露/门控/MCP 懒加载，
`ToolRuntimeMiddleware` 负责工具保护。中间件顺序 `[Skills, ToolRuntime]` 有意为之：
先由 Skills 决定"这一轮哪些工具可见 / 动态工具是谁"，再由 ToolRuntime 包住真正的执行。

### LLMFactory：解除厂商绑定的来龙去脉

教训来自 my-agent：`ChatTongyi(dashscope_api_key=...)` 被硬编进 `ChatAgent`、`ChatService`、`AIOpsService`
好几处（`my-agent-original/app/agents/chat_agent.py:31` 等），换模型就得改多处代码、还只能用通义。
`LLMFactory.create_llm` 统一造 `ChatOpenAI`，`model/api_key/base_url/temperature/streaming` 全从 `settings`
读、可逐参覆盖，于是 DashScope compatible-mode、美团 FRIDAY、本地 Ollama 等任意 OpenAI 兼容接口都能接。
一个刻意的小设计：`api_key` 为空时**构造期就 `raise`**，不让空 key 拖到第一次真实调用才以 401 暴露。

### 工具熔断三态 + 降级文案回喂模型

`tool_runtime.py` 是每工具独立的断路器，三态：`closed`（正常）→ 连续失败达 `failure_threshold` 转
`open`（直接短路、不再调工具）→ 过 `recovery_timeout_seconds` 转 `half_open`（放一次探针，成功回 closed、
失败立刻回 open）。`TOOL_POLICIES` 按工具给差异化策略（`tavily_search` 12s、`get_current_datetime` 2s 且
不重试……），`_is_retryable_exception` 只对超时 / 连接类 / 502/503/504/429 这类瞬时错误重试。

`ToolRuntimeMiddleware.awrap_tool_call` 是接法的关键：用一个 `result_cell` 把 `handler` 的**真实返回**
（`ToolMessage` / `Command`，Skills 激活等副作用都在里面）存下来，只把它的**文本形式**喂给
`safe_tool_execute` 做失败启发式判断。成功就原样返回真实结果（不破坏 Skills 的 `Command`）；
熔断打开 / 重试耗尽则**不抛异常**，而是把 `TOOL_FALLBACK_MESSAGES` 里的降级文案包成
`ToolMessage(status="error")` 回喂模型，让 Agent 降级续跑（"检索暂不可用，请基于现有信息继续"）
而不是整个循环崩掉。

## ③ 参考项目怎么做的

**my-agent（手写 StateGraph + tool_runtime）** —— `my-agent-original/app/agents/chat_agent.py`：
`StateGraph(AgentState)` 手接三个节点 `agent`（`call_model`）/ `tools`（`call_tools`）/ 条件边
`should_continue`，LLM 是 `ChatTongyi(...).bind_tools(tools)`。熔断是**直接写在 `call_tools` 节点里**：
`execution = await safe_tool_execute(tool_name, tool, tool_args)`（chat_agent.py:62）。
本项目**保留了 `tool_runtime.py` 原样**（断路器逻辑、每工具策略、可重试异常启发式一字未改），
只把"在图节点里直接调 `safe_tool_execute`"换成"在 `ToolRuntimeMiddleware` 里调"。

**Yuxi 的 create_agent 中间件栈顺序** —— `yuxi-reference/.../agents/buildin/chatbot/graph.py` 的
`_build_middlewares`：`[filesystem, save_attachments, SkillsMiddleware, subagent, summary, TodoList,
PatchToolCalls, ModelRetry, ImageInput, TokenUsage, approval]`，`SkillsMiddleware` 排在前部先决定工具可见性。
本项目照搬了"`create_agent` + 中间件栈"的架构，但只保留演示必需的最小两个中间件（Skills + ToolRuntime），
不引入 summary / todo / approval 等 Yuxi 多租户产品才需要的层。

## ④ 区别与取舍

- **为什么从手写 StateGraph 换 create_agent**：手写图要自己维护节点 / 条件边 / 消息累加，且 Skills 的
  披露 / 门控 / 激活拦截需要在"模型调用前"和"工具调用后"两个切面精确插手 —— 这正是 `AgentMiddleware`
  的 `awrap_model_call` / `awrap_tool_call` 钩子，手写 `call_tools` 节点里很难干净表达（尤其动态工具的
  `request.override`）。换 `create_agent` 后，图交给框架，能力都变成可插拔中间件，还白得流式等基建。
- **熔断做成中间件而不是包在工具函数里**：熔断是横切关注点，做成中间件能**对所有工具统一生效**，
  包括 Skills 懒加载进来的动态 MCP 工具；工具函数保持纯粹、可单测。my-agent 把它焊在 `call_tools` 节点里，
  只覆盖那个 agent 自己的工具，换 agent 就得重写一遍 —— 中间件把这份能力从具体 agent 里解耦出来。
- **超时 / 重试参数怎么定的**：不用一刀切，`TOOL_POLICIES` 按工具性质给：外部网络工具（`tavily_search`）
  给 12s + 1 次重试容忍抖动，纯本地 / 极快的 `get_current_datetime` 给 2s 且 0 重试（快失败不拖延），
  易抖的日志 / 告警类给更低的 `failure_threshold` 更快熔断。重试只认瞬时错误
  （`_is_retryable_exception`），业务性失败（如工具返回 `success=false`）不做无谓重试。

## ⑤ 遗留 / 记账

- **降级文案仍是英文**：`TOOL_FALLBACK_MESSAGES` 沿用自 my-agent 的 AIOps 场景（`query_log` /
  `query_prometheus_alerts` 等策略键也是那批遗留），本项目主链路是中文问答，这批文案与策略键
  日后应随接入的工具改成中文 / 对齐本项目工具名，属已知欠账。
- **系统提示词内置**：`DEFAULT_SYSTEM_PROMPT` 直接写在 `chat_agent.py`，与 Skills 披露段在
  `awrap_model_call` 里拼接；若要做多套人设 / 多 agent，提示词应外置（可复用 Skills 的模板化思路）。
- **单例组装**：`get_chat_agent` 是进程级单例，`.env` 改了要重启才生效；测试用
  `reset_singletons()`（dependencies.py）重置。
