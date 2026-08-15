# SQL 知识回流闭环（sql-knowledge-loop）

> 状态：当前设计方案。分支 `feat/sql-knowledge-loop`。

## 1. 定位

示例库 docstring 宣称"答对的问答经反馈接口入库"，但全仓无任何自动/半自动回流调用点
（回流只有前端手工录入）；评测失败 case 与示例库两张皮；平台数据源场景下术语/示例库
被 `sql_context_tool.py` 整体禁用（因两张表无作用域列，怕跨租户串数据）。

本设计把回流闭环真正接上，对应 roadmap §3.1「知识飞轮（单人版）」的最小可讲版本：

- **对话回流**：SQL 执行成功后前端一键沉淀为已验证示例（人工确认闸，不全自动防污染）；
- **评测回流**：评测失败 case 的 golden_sql 导入候选示例（verified=False），人工转正后生效；
- **作用域解禁**：示例/术语表加作用域列，平台数据源按 datasource_id 检索自己的知识；
- **指标闭环**：评测报告对比工具，产出 few-shot 增强前后准确率数字。

## 2. 设计

### 2.1 数据结构：复用两张表扩列，不建候选队列表

`verified` 天然表达候选（False）/转正（True）；转正 = verified 翻转的 upsert，
复用现有 `POST /api/sql-examples`，**无新端点，PROTECTED_ENDPOINTS 零新增**。
独立候选表会引入跨表拷贝与双向去重判断，示例量级几十条撑不起该成本
（与 workspace-lite 同款取舍）。

新列（SQLite/PG 均支持带 DEFAULT 的 ADD COLUMN，幂等窄迁移，无需重建表）：

| 列 | 两表 | 语义 |
|---|---|---|
| `datasource_id` INT NULL, index | 都加 | NULL=演示库/全局作用域 |
| `workspace_id` INT NOT NULL DEFAULT 0 | 都加 | 0=内置种子/无鉴权 demo（对齐 graph_triples 先例） |
| `source` VARCHAR(32) DEFAULT 'manual' | 仅示例表 | manual / chat / eval |
| `meta` JSON DEFAULT '{}' | 仅示例表 | chat:{session_id}；eval:{case_id,tags,pred_sql,error,report} |

去重升级：示例 add 从"全局同 question 覆盖"改为**按作用域覆盖**（平台数据源按
datasource_id，演示作用域按 workspace_id；否则平台示例会覆盖演示库同题示例——
正是要消除的串数据，鉴权开启后不同 workspace 的演示库同题也各自独立）；术语例外——表主键仍是
term（全局唯一），同一术语不能双作用域并存，挪作用域 = 一次带 datasource_id 的
覆盖写入（术语量级极小，不值得为此复合主键重建表）。

### 2.2 作用域检索

`sql_context_tool` 删除"平台数据源整体禁用"分支，改为：

- selection 有值（平台数据源）→ `datasource_id == selection.datasource_id` 且 verified；
- selection 无值（演示库）→ `datasource_id IS NULL AND workspace_id == 当前 workspace`
  （demo normalize 后 =0，**内置种子行为逐字节不变**）；
- 术语同构；无命中保留现有提示文案。

**few-shot 收紧为仅注入 verified=True**（现状未验证示例也注入并标"未验证"）：
候选（chat/eval 来源）是待审核知识，默认不进 prompt，人工转正后生效——
知识飞轮"候选→审核→生效"的完整语义，防污染。行为变更，commit body 记账。

### 2.3 对话回流信号：工具内直录 ContextVar

不解析 ToolMessage：结果 JSON 里没有 SQL，SQL 在 AIMessage.tool_calls 里，
流式下 tool_call_chunks 分片到达聚合不可靠。工具内直录是项目已有先例
（`current_selection` 路由写工具读、`current_sources` 工具写服务读）。

- 新模块 `app/text2sql/feedback.py`：`SQLExecutionRecord` + `use_sql_recorder()`
  contextmanager + `record_sql_execution()`（无 recorder 时空操作，单测不受影响）
  + `latest_successful_sql()`。
- `sql_tool.py` 两条成功路径（平台 runtime / 演示 sqlite）return 前 record；
  **多轮多次执行取最后一次成功**（即用户最终看到的结果对应的 SQL）。
- `chat_service.py`：chat/chat_stream 进入 recorder；流式在 sources 事件后
  yield `{"type": "sql_result", "data": {question, sql, row_count, columns, datasource_id}}`；
  非流式返回值加可选 `sql_result` 键。
- 前端旧代码忽略未知 SSE 类型（向后兼容）；ChatView 处理 sql_result，
  「沉淀为示例」按钮 → 现有 POST /api/sql-examples（verified=true + datasource_id + source=chat）。
- 兜底：若集成测试发现线程池丢 ContextVar，退路是在 ToolRuntimeMiddleware.wrap_tool_call
  里按工具名 + json.loads 键集判定记录。

### 2.4 评测失败回流：CLI 直写 DB

`evals/text2sql/export_failures.py`：按 `get_example_store` 同款装配直写 DB，
**不加 HTTP API**（离线运营动作，API 反而要背鉴权+上传）。

**沉淀内容：question + golden_sql 作候选（verified=False, source='eval'）；
pred_sql/error 不做示例、进 meta 作错误模式标注**——golden_sql 是人工标注的正确映射，
正是示例库的知识本体；模型失败恰好说明该 question 的 few-shot 缺失；pred_sql 是错的，
进 prompt 有污染风险，放 meta 供审核时并排展示"模型当时怎么错的"。

规则：只导 correct=False；同 scope 同 question 已 verified=True 则跳过（不降级已有知识）；
按 (question, scope) 覆盖幂等。参数 `--report`（默认 execution_latest.json）、
`--datasource-id`、`--dry-run`。转正：KnowledgeView 候选分组 + 转正/丢弃按钮。

### 2.5 评测报告对比工具

`evals/text2sql/compare_reports.py`：两份报告 JSON → Markdown 差异表
（总体准确率、按题型标签分解提升/回退、case 级翻转列表），stdout 或 `--out`。
用法：基线跑 → 导入失败 case 并转正 → 再跑 → 对比出数字。

## 3. 文件结构

```
新增：
  app/db/knowledge_migration.py        # 幂等窄迁移（沿用 graph_migration 先例）
  app/text2sql/feedback.py             # ContextVar SQL 执行记录器
  evals/text2sql/export_failures.py    # 评测失败 → 候选示例
  evals/text2sql/compare_reports.py    # 报告对比 → Markdown
修改：
  app/db/models.py                     # 两模型加列
  app/db/repositories.py               # 两仓储收发新字段
  app/db/engine.py                     # init_db 先跑 upgrade_knowledge_schema
  app/text2sql/examples.py             # 作用域字段 + 按 scope 去重 + verified-only 检索
  app/text2sql/terminology.py          # 作用域字段 + 按 scope 去重
  app/agents/tools/sql_tool.py         # 成功路径 record
  app/agents/tools/sql_context_tool.py # 删禁用分支改 scope 检索
  app/services/chat_service.py         # recorder + sql_result 事件
  app/api/routes_knowledge.py          # POST 加 datasource_id 归属校验 + source 白名单
  frontend/.../ChatView.vue            # sql_result + 沉淀按钮
  frontend/.../KnowledgeView.vue       # 候选分组 / 转正 / eval meta
```

## 4. API 变化

- `POST /api/sql-examples` 请求体加 `datasource_id: int | None`（None=演示作用域；
  有值时经 datasource_service 校验归属，非本工作空间 404）与
  `source: "manual" | "chat"`（白名单，eval 只能由 CLI 写入）。
  响应加 `datasource_id / verified / source` 字段。
- `GET /api/sql-examples` / terminology 不变（前端本地按 verified 分组）。
- SSE 新事件类型 `sql_result`（旧前端忽略未知类型，向后兼容）。
- 无新增端点；`PROTECTED_ENDPOINTS` 不动（tests/test_auth.py 零改动）。

## 5. 与参考项目的差异

- SQLBot 的术语库是全局单层，示例（training）按数据源隔离；本项目两者统一按
  (workspace, datasource) 作用域——datasource_id 已是租户内隔离，workspace_id
  兜住演示库路径（datasource NULL）在鉴权开启后的归属。
- AIDE 知识飞轮的"独立命中 + 审核双重门槛"在本项目退化为单人版：候选（chat 一键
  沉淀 = 用户即审核人；eval 导入 = golden_sql 即标注）+ verified 翻转即晋升，
  无自动蒸馏——个人项目没有流量支撑自动晋升统计。

## 6. 已知欠账（进 commit body）

- ExampleStore/TermStore 单例启动时全量加载内存，多 worker 跨进程写不刷新
  （现状已有，本轮不扩）。
- sync 工具的 ContextVar 传播依赖 langchain v1 对 sync 工具在执行线程上继承上下文
  （current_selection 同机制已在生产路径生效）；若失效走 §2.3 兜底。
