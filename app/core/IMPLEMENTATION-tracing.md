# Langfuse 调用链追踪速读（tracing.py + ChatAgent 单点接入）

> 定位：给"一句中文问题 → 工具编排 → 回答"这条链路补一层**可观测性**，把一轮对话里
> 发生的模型调用、工具调用、中间件包裹、耗时与错误收拢到 Langfuse 的一条 trace 里。
> 设计基调对齐 roadmap P2「接入成本低，可作加分项」：**默认关闭、零侵入、缺配置绝不影响主流程**。
> Agent 主链路见 [../agents/IMPLEMENTATION-agent-core.md](../agents/IMPLEMENTATION-agent-core.md)。

核心文件：

- `tracing.py` —— `get_langfuse_callbacks() -> list`，唯一对外入口；进程内单例缓存 handler
- `settings.py` —— `langfuse_enabled` / `langfuse_public_key` / `langfuse_secret_key` / `langfuse_host`
- 接入点：`../agents/chat_agent.py` 的 `ChatAgent.chat` / `chat_stream`（graph 调用传 `config={"callbacks": ...}`）
- 依赖：`pyproject.toml` 的 `langfuse>=2`（实测装到 v4.14）

## ① 功能与配置方法

启用后，每次 `ChatAgent.chat` / `chat_stream` 都会在 Langfuse 生成一条 trace，可回看这一轮的
模型输入输出、工具调用链、各步耗时与错误位置，比翻后端日志更容易定位"问题出在模型、工具还是图流程"。

配置四个变量（`.env` 里设，`.env.example` 有注释块）：

```bash
LANGFUSE_ENABLED=false                        # 总开关，必须显式 true 才启用
LANGFUSE_PUBLIC_KEY=                           # Langfuse 项目里获取
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com       # 云端；自建实例填自己的地址
```

启用条件是**三者同时满足**：`LANGFUSE_ENABLED=true` 且 public / secret key 均非空。
云端直接用官方 `https://cloud.langfuse.com`；自建（docker 部署 Langfuse）把 host 换成自己的实例地址即可，
其余不变。只要 key 缺失或开关为 false，就自动退化为"不追踪"，聊天功能完全不受影响。
配置是进程级单例，改了 `.env` 需重启后端才生效。

## ② 实现原理

**LangChain callback 机制**：LangGraph 执行图时会沿途派发 `on_chain_start` / `on_llm_start` /
`on_tool_start` / `on_*_end` 等回调事件。Langfuse 的 `CallbackHandler`（一个 `BaseCallbackHandler`）
订阅这些事件，经 OpenTelemetry 把它们翻译成 Langfuse 的 trace + 嵌套 observation。
关键接法只有一处：把 handler 放进 `config={"callbacks": [...]}` 传给 graph 的 `ainvoke` / `astream`，
框架会**自动向下传播**到图里所有节点、工具、中间件，无需逐层手接。

**trace / span 与我们图里的节点对应**：一次 `chat()` = 一条 trace（根）；`create_agent` 图里的
模型调用节点 = 一个 generation observation（带 token 用量）；每个门控工具（`execute_sql` /
`read_skill` / `tavily_search`）的一次调用 = 一个 span，嵌套在 trace 下；`SkillsMiddleware` /
`ToolRuntimeMiddleware` 的包裹关系也体现在这层嵌套里。多轮 tool-calling 就是同一 trace 下的多个 span。

**v4 的构造方式**（`tracing.py::_build_callbacks`）：先用凭证初始化 `Langfuse(public_key, secret_key, host)`
客户端（它把自己注册成全局 OTEL provider），再无参构造 `CallbackHandler()` 复用该全局客户端 ——
这与 langfuse v2「`CallbackHandler(public_key=...)` 直接吃 key」不同，导入路径也从 v2 的
`langfuse.callback` 变成 v3/v4 的 `langfuse.langchain`。本项目装的是 v4，按 v4 路径接。

**惰性单例 + 失败降级**：`get_langfuse_callbacks()` 在未启用 / 缺 key 时直接 `return []`，**连 langfuse 都不 import**，
未安装 / 未配置零开销；启用时才 import + 构造，结果缓存进模块级 `_callbacks_cache`，进程内复用同一 handler；
构造抛错（host 错、SDK 不兼容、网络问题）则 `logger.warning` 后返回 `[]`，让本轮如同未接追踪一样正常跑完。

## ③ Yuxi 怎么接的

Yuxi 把 Langfuse 做成**独立服务层**，比本项目重，但注入位置的思路一致：

- `backend/package/yuxi/services/langfuse_service.py`：可选依赖 `try: from langfuse import Langfuse;
  from langfuse.langchain import CallbackHandler`（第 11-16 行，import 失败置 None）；`is_langfuse_enabled()`
  查 `LANGFUSE_ENABLED` 环境变量 + key 是否齐全；`get_langfuse_client()` 用 `@lru_cache` 做单例、
  构造失败 `warning` 返回 `None`；`build_run_context()` 用 `client.create_trace_id(seed=request_id)` 造 trace_id，
  再 `CallbackHandler(trace_context={"trace_id": trace_id})`，连同 metadata / tags 打包成 `LangfuseRunContext`。
  它还把 Yuxi 用户映射成 `langfuse_user_id`、会话线程映射成 `langfuse_session_id`，并有 `submit_user_feedback_score`
  把点赞/点踩同步成 Langfuse score、`flush_langfuse` 刷事件。
- `backend/package/yuxi/agents/base.py`：`invoke_messages` / `stream_messages` / `_stream_input_with_state`
  把 `callbacks` / `metadata` / `tags` 从 kwargs 塞进 `input_config`，再 `input_config["callbacks"] = list(callbacks)`
  传给 `graph.ainvoke` / `graph.astream` / `graph.astream_events`（第 188-201、294-306 行）——
  和本项目一样，**注入点选在 graph 调用层**，不在模型工厂或中间件里。
- 用户文档：`docs/advanced/langfuse-integration.md`（定位 / 配置 / 查看路径）。

## ④ 取舍

- **为什么默认关闭**：tracing 是可选增强项而非启动前置依赖。缺配置绝不能拖垮 demo 主链路，
  所以零配置默认即"关"，且关的时候连 import 都不做（零开销）。这也和 Yuxi「key 不全就自动退化不启用」一致。
- **为什么在 ChatAgent 单点接入，而不是 LLMFactory / 中间件**：
  - 放 `LLMFactory` 只能看见**单次模型调用**，看不到工具调用、中间件包裹和多轮编排 ——
    那只剩 generation、丢了整条调用链的嵌套，恰恰丢掉了"调用链追踪"的价值。graph 调用才是一条 trace 的天然根。
  - 做成**中间件**要多挂一层，且中间件按节点触发，容易产生重复 / 碎片化的 trace，还把"观测"耦进了
    "Agent 能力"这一层。callbacks 是 LangChain 官方的横切观测通道，在 graph 入口（config）传一次就自动向下贯穿
    所有节点与工具 —— **一个注入点 = 每轮一条干净 trace**。Yuxi 也接在 graph 调用层（base.py）而非模型工厂，结论相同。
- **为什么失败降级为空**：观测层必须严格从属于主流程。客户端初始化 / handler 构造失败时返回 `[]`，
  让 graph 跑得和"没开追踪"一模一样 —— 绝不让一个监控依赖把对话搞挂。

**遗留 / 记账**：本项目只接了最小闭环（单点 handler）。Yuxi 的 per-request `trace_id`、
`user_id` / `session_id` 维度映射、tags、反馈 score 同步都**未移植** —— 因为 demo 暂无用户体系 / 会话线程 /
反馈接口，硬接这些属于过度设计。待日后有用户与会话模型，可在此扩展 metadata / tags（接口天然在 `config` 里）。
