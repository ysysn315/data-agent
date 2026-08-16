# 分析 Agent 实现说明（P-O-R 工作流）

`AnalysisAgent`（`app/agents/analysis_agent.py`）把一个宽泛的分析请求，跑成
**Plan → Operation → Reflection → Report** 四阶段的 LangGraph StateGraph，产出一份
结构化 Markdown 报告。它是"保留 my-agent 核心能力"清单里最后一块迁移项——原版
`AIOpsAgent`（`my-agent-original/app/agents/aiops_agent.py`，283 行）的 P-O-R 骨架
保留，领域硬编码与执行方式全部重构，并接进本项目的技能（Skills）与异步任务框架。

> 一句话定位：ChatAgent 回答"一个问题"；AnalysisAgent 把"一个分析任务"拆成多步、
> 每步各跑一次 ChatAgent，最后反思并汇总成报告。

---

## 一、功能与用法

### 1. 同步小模式（步数 ≤ 2，秒级返回）

```bash
POST /api/analysis
{"question": "分析各州的销售额分布并给出建议"}
```

返回 `{report, plan, steps}`：`report` 是完整 Markdown；`plan` 是规划出的步骤；
`steps` 是各步摘要。路由在 `app/api/routes_analysis.py`，`max_steps` 被夹到 2
（`SYNC_MAX_STEPS`）——同步只做小分析，Planner 无法产出合法计划时返回 400。

### 2. 异步长任务（多步 + 进度可订阅）

步数多或耗时长的分析走异步任务框架（D 轮的 arq + Redis Streams）：

```bash
POST /api/tasks   {"type": "run_analysis_task", "params": {"question": "..."}}  -> {task_id}
GET  /api/tasks/{id}          # 查状态，done 后 result.report 即完整报告
GET  /api/tasks/{id}/events   # SSE 进度流
```

SSE 事件序列（`type`/`payload.phase`）：

```
started → planning → step 1/N → step 2/N → … → reflecting → reporting → done
```

发现缺口触发补充时，中间会多一段 `… → reflecting → step 3/N → reflecting → …`。
每条 `step` 事件的 payload 带 `{index, total, goal, sql, answer_preview}`，`done`
带各步摘要 `steps`——前端只认这一种 `TaskEvent` schema（`app/tasks/events.py`）。

---

## 二、实现原理

### 1. 三阶段状态机

`AnalysisState`（`TypedDict`）承载 `question / plan / step_results / reflection /
supplement_used / report`。图拓扑（`_build_graph`）：

```
planner → operation → reflection ─┬─(还有没跑的步骤)→ operation
                                  └─(无)────────────→ report → END
```

- **Planner**（`planner_node`）：LLM 把请求拆成 2~N 步 JSON 计划，每步
  `{goal, tool_hint}`。解析走 `_extract_json`（容忍 ```json 围栏与前后缀文字）
  +`_parse_plan`（校验非空、每步有 goal、步数下限）。**解析失败重试一次，再失败
  抛 `RuntimeError`**（`_plan_with_retry`）——计划是后续一切的根，必须显式失败而非糊弄。
- **Operation**（`operation_node`）：遍历 `plan` 里**尚未执行**的步骤
  （`range(len(step_results), len(plan))`），每步调 `_run_step` 跑一次 ChatAgent，
  用 `_extract_step_output` 从消息流里抽出（回答文本, SQL 列表, 工具轨迹）。
- **Reflection**（`reflection_node`）：LLM 审查全部步骤，输出 JSON
  `{assessment, conclusion, need_more, supplement}`。反思是**尽力而为**的——JSON 解析
  失败不报错（不同于 Planner），退化为"用原文当结论、不补充"，避免格式抖动搞崩整条链。
- **Report**（`report_node`）：`_render_report` 在代码里拼固定结构的 Markdown。

### 2. 补充步骤的防循环设计

Reflection 发现缺口时，把补充步骤 append 进 `plan` 并置 `supplement_used=True`；
条件边 `_route_after_reflection` 只看 `len(plan) > len(step_results)` 就回
Operation 补跑那一步。**关键防线是 `supplement_used` 标志**：第二次进 Reflection 时，
即便 LLM 仍然嚷着 `need_more=True`，`can_supplement` 也会因该标志为假而拒绝加步——
**全程至多补充一次**，杜绝 planner↔reflection 无限打转（原版靠 `iteration>=6` 兜底，
见 §三）。测试 `test_reflection_triggers_one_supplement_then_stops` 正是钉这条路径。

### 3. 与 ChatAgent 的复用关系：每个 Operation 步骤 = 一次完整技能三段式

`_run_step` 直接跑 `chat_agent.graph.ainvoke(...)`（与 `app/tasks/worker.py` 的
`run_chat_task` 同款用法）。这意味着**每一步都完整享有 ChatAgent 的能力栈**：
`SkillsMiddleware` 的渐进式披露 → `read_skill` 激活 → 门控工具解锁的三段式、
`ToolRuntimeMiddleware` 的重试/熔断/降级，全部原样生效。AnalysisAgent 自己
**不持有任何业务工具**，只负责"拆解—调度—反思—汇总"这层编排。注入的 `llm` 仅供
Planner/Reflection 做规划与复盘，与 Operation 的执行 LLM（ChatAgent 内部）解耦。

进度经 `on_event(TaskEvent)` 回调上报：`_emit` 把 `phase` 塞进 payload，异步任务侧
（`run_analysis_task`）把回调接到 `TaskService.publish_event`，事件即透传给 SSE。
构造期注入 `llm` 与 `chat_agent`，测试可全部换成假模型（见 `tests/test_analysis_agent.py`
的 `FakeScriptedLLM` / `FakeChatAgent`），离线跑通、不碰真 LLM。

---

## 三、my-agent 原版怎么做的（保留 / 重构）

原版 `AIOpsAgent`（`my-agent-original/app/agents/aiops_agent.py`）也是 P-O-R：

- `_create_graph`（`aiops_agent.py:45-61`）：`planner→operation→reflection`，
  `should_continue` 条件边 `continue`→回 planner / `end`→END。
- `planner_node`（`:63-125`）：**领域硬编码**——用 `query_prometheus_alerts /
  query_log / query_internal_docs` 的调用计数决定下一步指令，AIOps 专用。
- `operation_node`（`:127-184`）：`tool_llm = base_llm.bind_tools(tools)` + 手写
  `safe_tool_execute` 循环，把结果拼成 `tool=…\nstatus=…` 文本塞进 `past_steps`。
- `reflection_node`（`:186-225`）：LLM 回 `"继续"` 或直接吐一段自由文本"最终报告"。
- `should_continue`（`:227-232`）：`response` 有值或 `iteration>=6` 就 END。

**保留**：P-O-R 三节点骨架 + 条件边、"逐步收集结果再反思"的思路。

**重构**：

| 维度 | 原版 AIOpsAgent | 本项目 AnalysisAgent |
|---|---|---|
| 计划 | AIOps 硬编码规则（`:73-97`） | 通用 LLM JSON 多步计划 + 重试/显式报错 |
| 执行 | `bind_tools`+`safe_tool_execute`（`:153-184`） | 复用 ChatAgent 一轮（技能三段式全生效） |
| 循环 | 回 planner，`iteration>=6` 兜底（`:230`） | 至多一次补充，`supplement_used` 硬防循环 |
| 报告 | Reflection LLM 自由文本（`:222-223`） | 代码拼接固定结构 Markdown |
| 进度 | `analyze_stream` 手写 json 帧（`:249-283`） | `on_event(TaskEvent)` 复用异步任务事件模型 |
| LLM | 绑死 `ChatTongyi`（`:24-29`） | 构造注入 `BaseChatModel`，测试可假 |

---

## 四、取舍

**为什么 Reflection 只允许一次补充？** 反思驱动的"再查一步"最容易退化成 LLM 反复
说"还不够"的死循环。原版用 `iteration>=6` 这类魔法数兜底，本质是"多转几圈再放弃"，
既费 token 又不可预测。分析任务的价值在**收敛出报告**而非无限深挖；给一次补充机会
能救"计划漏了关键一步"的常见情况，`supplement_used` 又把上界钉死为可证明的常数——
比迭代计数更强的保证，也更好测。

**为什么复用 ChatAgent 而不是给分析建独立工具集？** 单一职责：技能三段式（披露/门控/
激活）、SQL 门控工具、工具熔断降级，都已在 ChatAgent + 中间件里实现且被测试覆盖。
若另起一套 `bind_tools`（原版做法），等于把这些能力重造一遍、还要各自维护。让每个
Operation 步骤"就是一次完整对话"，分析层只做编排——新增技能自动被分析复用，零改动。

平台数据源选择由 `AnalysisRequest.datasource_id` 或异步任务入队参数承载；API/worker 在整个
图执行期间设置 `use_datasource` 与 GraphScope，并先校验数据源归属。`workspace_id` 由服务端
覆盖，模型不会获得切库参数；评测任务仍固定使用演示库。

**为什么报告在代码里拼接而不是让 LLM 一次性生成？** 报告的**结构**（概述/各步发现/
结论与建议/附：SQL 清单）应当确定、可断言、可被前端稳定渲染；LLM 一次性生成整篇
容易漏段、错格式、把已执行的 SQL 编造或遗漏。因此 `_render_report` 用代码保证骨架与
SQL 清单的**忠实性**（SQL 直接取自工具轨迹，不经 LLM 转述），只把需要"分析文笔"的
`结论与建议` 交给 Reflection 的产出填充。结构确定性与内容智能各归其位，测试
`test_three_step_plan_runs_and_builds_report` 才能对四大段标题与 SQL 清单逐条断言。
