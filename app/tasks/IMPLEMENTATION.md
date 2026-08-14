# 通用异步任务框架 + 事件流（ARQ + Redis Streams）

把"跑一轮对话""跑一遍评估"这类耗时操作从请求线程里剥离到后台 worker：API 立即返回
`task_id`，前端用 SSE 订阅进度，任务在 arq worker 里执行并把每一步进度写进 Redis Stream。

模块清单：
- `app/tasks/events.py` —— 进度事件模型 `TaskEvent(type/message/progress/payload)`
- `app/tasks/service.py` —— `TaskService`：入队 / 状态 / 事件流（XADD/XRANGE）+ 连接构造
- `app/tasks/worker.py` —— arq `WorkerSettings` + chat / eval / analysis 三类任务
- `app/api/routes_tasks.py` —— 提交 / 查询 / SSE 三个端点
- `get_task_service()`（`app/core/dependencies.py` 末尾）—— 生产环境单例注入

---

## 一、功能与用法

**启动 worker**（连接从 `settings.redis_*` 组装，无需额外配置）：

```bash
arq app.tasks.worker.WorkerSettings
```

**提交任务**（返回 `task_id`）：

```bash
# 后台跑一轮对话
curl -X POST localhost:9900/api/tasks -H 'Content-Type: application/json' \
  -d '{"type":"chat","params":{"question":"SP 州订单额多少","session_id":"s1"}}'
# 后台跑 Text-to-SQL 评估（前 5 例）
curl -X POST localhost:9900/api/tasks -H 'Content-Type: application/json' \
  -d '{"type":"eval","params":{"limit":5,"model":null}}'
# => {"task_id":"3f2a...e1"}
```

**查询状态 + 结果**：

```bash
curl localhost:9900/api/tasks/3f2a...e1
# => {"status":"done","result":{"accuracy":0.8,...},"created_at":...}
```

**订阅进度事件流（SSE）**：

```bash
curl -N localhost:9900/api/tasks/3f2a...e1/events
# data: {"type":"started","message":"开始评估：5 个用例",...}
# data: {"type":"progress","message":"第 1/5 例 ✓","progress":0.2,...}
# ...
# data: {"type":"done","message":"评估完成，执行准确率 80.00%","payload":{"accuracy":0.8}}
```

任务状态机：`queued → running → done | failed`。事件类型：`started / progress / done / error`，
其中 `done / error` 为终结事件，SSE 读到即关闭连接。

---

## 二、实现原理

**arq 任务生命周期。** 入队时 `TaskService.enqueue` 做两件事：把元数据写进 Redis Hash
（`task:{id}:meta`，status=queued）、用 **task_id 作为 arq job_id** 调 `pool.enqueue_job`。
worker 进程轮询队列取到 job，把 `ctx["job_id"]`（即 task_id）和 `ctx["redis"]`（连接池）注入
任务协程。任务据此 `mark_running` → 逐步 `publish_event` → `mark_done/mark_failed`。task_id
复用为 job_id，任务无需额外参数就知道"我是谁"，能直接回写自己的状态与事件。

**Redis Streams 做事件流，为什么优于 pub/sub（历史回放）。**
- **pub/sub 是"发了就没"**：消息不落地，订阅者不在线就丢。SSE 客户端一旦断连重连，断连
  期间的进度全部丢失；任务先跑、前端后订阅，早期进度也拿不到。
- **Streams 持久化 + 单调 seq 游标**：`XADD` 落盘（带 TTL），每条事件有 `<ms>-<seq>` id。
  Service 内部的 `read_events(after_seq)` 能按游标增量读取，`XRANGE - +` 也能从头回放历史进度。
  当前 HTTP SSE 端点每次都从 `0-0` 开始，正好覆盖"任务已在跑，前端才来订阅"；它尚未接收
  `Last-Event-ID/after_seq`，SSE 帧也没有 `id:`，因此**不等于断点续传**。`stream_sse` 是一个
  "内部游标读取 → 吐帧 → 轮询"循环，读到终结事件或元数据落终态即补发 `done` 并关闭。
- **代价**：Stream 占内存，需 TTL（本实现 24h）/MAXLEN 管理；pub/sub 零存储。当"进度必须
  可靠、可回放"时，这点存储成本值得。

---

## 三、Yuxi 怎么做的

参考实现（只读）在 `/Users/ysn/projects/yuxi-reference`：

- **Worker 定义 / 任务注册**：`backend/package/yuxi/services/run_worker.py` —— `WorkerSettings`
  类用 `functions = [process_agent_run]` 注册任务，配 `on_startup/on_shutdown`、`max_tries`、
  `job_timeout`、`redis_settings = get_arq_redis_settings()`。入口 `backend/server/worker_main.py`
  只 re-export `WorkerSettings`，供 `arq ...WorkerSettings` 启动。本项目 `worker.py` 同构。
- **入队**：`backend/package/yuxi/services/agent_run_service.py::enqueue_agent_run` —— 
  `queue = await get_arq_pool(); await queue.enqueue_job("process_agent_run", run_id, _job_id=f"run:{run_id}")`。
  本实现照搬"task_id 即 job_id"的做法。
- **事件推送（Redis Streams）**：`backend/package/yuxi/services/run_queue_service.py` —— 
  `append_run_stream_event` 用 `redis.xadd(key, fields)` 追加事件并 `expire` TTL；
  `list_run_stream_events` 用 `xrange(key, min=(after_seq, max="+")` 增量读，游标归一
  `normalize_after_seq`。本实现的 `publish_event/read_events` 与之同形。
- **SSE 读取**：`agent_run_service.py::stream_agent_run_events` —— 轮询 `list_run_stream_events`，
  `format_sse(envelope, event=type, event_id=seq)`，读到 `end` 事件或 DB 落终态即结束。
- **RedisSettings 组装**：`backend/package/yuxi/storage/redis/manager.py::get_arq_redis_settings`。

差异：Yuxi 把 run 状态存 **Postgres（AgentRun 表）**、用 pub/sub 单独传"取消信号"、事件 envelope
带 `schema_version/thread_id`。本项目只做通用框架，状态存 Redis Hash、省掉取消通道，保持精简。

---

## 四、取舍

**为什么 arq 不是 celery。** arq 原生 asyncio，和本项目 FastAPI + LangGraph 的全异步栈同构——
任务里能直接 `await` Agent / LLM / Redis，无需 gevent/线程池桥接（celery 对 asyncio 是外挂）。
依赖也最省：arq **只要 Redis**，而项目已用 Redis 存会话，零新增 broker；celery 通常还要
RabbitMQ 或独立 result backend，运维更重。代价是 arq 的编排/路由/监控生态不如 celery 成熟，
但本项目用不到。

**数据源边界。** 当前 `run_chat_task`/`run_analysis_task` 的入队参数和 worker 都没有携带
`workspace_id + datasource_id`，所以后台 Agent 使用演示数据源；平台数据源选择目前只贯通
同步/流式 Chat。后续接入时必须在入队前校验工作空间归属，并把选择写入 worker 请求级上下文，
不能让模型自行传数据源 ID。

**为什么 Streams 不是 pub/sub。** 见"实现原理"：进度事件要求迟到订阅仍可回放，pub/sub
"发了就没"做不到。Streams 还为未来的断点续读保留了 seq 游标，但当前 API/前端没有贯通重连游标；
取消信号那种"一次性、丢了也无所谓"的场景才适合 pub/sub（Yuxi 正是这么分的）。

**为什么任务状态放 Redis 不入库（与 D1 持久化层的边界）。**
- 任务状态/进度是**运行时瞬态**：`queued→running→done` 秒级高频写、事件流每步一写，且只在任务
  存活的窗口内有意义。放 Redis（Hash + Stream + TTL）读写快、自动过期，且 worker 与 SSE 本就都
  连着 Redis，链路最短。
- **D1 持久化层（关系库）管的是业务事实**：对话消息、SQL 示例、技能/MCP 配置等需长期留存、可查询
  可关联的数据。任务执行的中间态不是业务事实，落库只会徒增 schema / 迁移 / 清理负担。
- **边界划法**：任务的"产物"若需长期留存（如对话最终回答），由任务在完成时写入其**业务归属**
  （如 `SessionStore` / 未来的 DB）；任务自身的 queued/running/事件流只存 Redis，随 TTL 自然消亡。
  即 D1 管"事实沉淀"，D 轮框架管"运行时事件"，互不越界。
