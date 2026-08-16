# SQL 示例库 + 术语库（知识库）实现说明

> roadmap P1-3（SQL 示例库 / 数据训练）+ P1-4（术语库）。
> 对标 SQLBot 的「越问越准」运营闭环：把历史问答与业务口径沉淀下来，回灌进
> Text-to-SQL 的 prompt，让 Agent 用得越久越准。

## 一、功能与用法

### 1.1 两个库

- **SQL 示例库**（`examples.py::ExampleStore`）：存 `question → SQL` 对。生成 SQL 前
  按问题检索出 top-3 相似示例作 few-shot 参考。持久化 `save_dir/sql_examples.json`。
- **术语库**（`terminology.py::TermStore`）：存业务术语 `{term, synonyms[], definition,
  sql_hint?}`。问题命中术语即注入「统一计算口径」，避免同一指标各算各的。
  持久化 `save_dir/terminology.json`。

两库都内置种子（示例 5 条真实可执行；术语为 REQUIREMENTS §5.2 的 GMV / 复购率 / 客单价），
首次启动即可演示，无需先训练。

### 1.2 反馈闭环怎么用

闭环的关键是**答对的问答要能回流入库**（feat/sql-knowledge-loop 起已完整落地，
三条回流路径 + 人工确认闸）：

1. **对话一键沉淀**：用户提问 → Agent 生成并执行 SQL → 前端出现「沉淀为示例」
   按钮 → 用户确认后 `POST /api/sql-examples`（`verified:true, source:'chat'`）。
   信号来自 `app/text2sql/feedback.py` 的 ContextVar 记录器：`execute_sql` 成功路径
   `record_sql_execution()`，`ChatService` 流末以 `sql_result` SSE 事件下发
   （多轮自纠取最后一次成功）。
2. **评测失败导入**：`python -m evals.text2sql.export_failures [--dry-run]` 把报告里
   失败 case 的 `question + golden_sql` 导入为候选（`verified:false, source:'eval'`）；
   `pred_sql/error` 只进 meta，知识管理页审核时并排展示"模型当时怎么错的"。
3. **人工转正闸**：候选（verified=False）**不进 few-shot**——`ExampleStore.search`
   默认 `verified_only=True`。知识管理页候选分组里「转正」= 同作用域覆盖写入
   `verified:true`，此后参与注入。评测报告对比（`evals.text2sql.compare_reports.py`）
   可量化转正前后的执行准确率变化。

示例越攒越多，命中率越高，这就是「越问越准」。术语库同理：把公司黑话与口径
（`POST /api/terminology`）一次维护好，之后所有相关问题都按同一口径算。

### 1.2.1 作用域（平台数据源解禁）

示例/术语带作用域列（`datasource_id` / `workspace_id`，见 `app/db/knowledge_migration.py`
幂等窄迁移）：`datasource_id=NULL` 是演示库全局作用域（`workspace_id=0` 为内置种子）；
平台数据源请求只检索 `datasource_id=本数据源` 的知识，跨作用域不串。`sql_context_search`
此前在选了平台数据源时整体禁用（怕全局知识串入租户），作用域化后改为分域检索。
去重键（作用域内唯一）：示例按 `(question, datasource_id, workspace_id)`；
术语按 `(term, datasource_id, workspace_id)`（表主键换自增 id，同 term 可在
不同作用域各自存在，跨作用域互不覆盖）。知识管理 API 的 GET/DELETE 按
workspace 过滤/校验归属（匿名视为 ws=0 只见演示数据），跨租户记录视同不存在。

### 1.3 API 一览（`app/api/routes_knowledge.py`，挂 `/api`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST/DELETE | `/api/sql-examples[/{id}]` | 列出 / 反馈入库 / 删除示例；POST 可带 `datasource_id`（校验归属）与 `source`（manual/chat 白名单） |
| GET/POST/DELETE | `/api/terminology[/{term}]` | 列出 / 新增更新 / 删除术语；POST 可带 `datasource_id` |

### 1.4 门控工具

`sql_context_search(question)`（`app/agents/tools/sql_context_tool.py`）**一次**返回
两段：命中术语解释 + top-3 相似示例；两段都空给明确提示。它是 sql-generation 技能
声明的门控工具（`dependencies.tools`），激活该技能后才对模型可见。SKILL.md 正文「④」
小节要求模型生成前先调用它。

## 二、实现原理

### 2.1 词元重叠打分（检索）

`ExampleStore.search` 复用 `app/skills/service.py::SkillService._tokenize`（jieba 分词、
小写、过滤单字符标点），保证与技能匹配同一套中文切分口径。打分 = 候选示例 question 的
词元集合 ∩ 查询词元集合的大小，降序取 top-k，`score=0` 不返回。零外部依赖、可解释、
几十条量级下够快够准。

术语命中（`TermStore.match`）更简单：术语或任一同义词是问题的**子串**（大小写不敏感），
对齐 SQLBot 的 `ILIKE '%word%'` 子串匹配语义。

### 2.2 门控工具接入

沿用 Skills v2 的三段式（`docs/openspec/skills-system.md`）：工具在
`dependencies.get_chat_agent` 构建期注册进 `gated_tools`（langchain v1 要求门控工具
构建期就在 ToolNode 里），默认对模型隐藏；模型 `read_skill("sql-generation")` 激活后，
该技能 `dependencies.tools` 里声明的 `sql_context_search` 才变可见。合并成一个工具
（而非术语、示例各一个）省一轮模型调用。

### 2.3 JSON 存储

参考 `app/mcp/service.py` 的注册表写法：内存 `list[dict]` + 原子写（tmp 文件 →
`rename`）。文件损坏时显式抛错而非静默清空。种子只在「无持久化文件」时灌入——已有
文件（哪怕被清空）尊重现状，不重复灌种。单例在 `app/core/dependencies.py` 的
`get_example_store / get_term_store`，与其它服务一致从依赖注入取。

## 三、SQLBot 是怎么做的

### 3.1 data_training（SQL 示例 / 数据训练）

`sqlbot-reference/backend/apps/data_training/`：

- **存储**：`models/data_training_model.py` 的 `DataTraining` 表，字段含 `question`、
  `description`（即期望 SQL/答案）、`embedding`（pgvector 向量列）。
- **召回**：`curd/data_training.py::select_training_by_question` 双路——① 文本
  `ILIKE` 双向子串匹配；② `EMBEDDING_ENABLED` 时用 pgvector 余弦距离
  `1 - (embedding <=> :q)` 召回，阈值 `EMBEDDING_DATA_TRAINING_SIMILARITY`、
  top `EMBEDDING_DATA_TRAINING_TOP_COUNT`。
- **注入**：`get_training_template` 把命中项 `to_xml_string` 成 `<sql-examples>` XML，
  填进 `get_base_data_training_template()` 拼进 system prompt。

### 3.2 terminology（术语库）

`sqlbot-reference/backend/apps/terminology/`：

- **模型**：`models/terminology_model.py` 的 `Terminology` 用父子行表达同义词——
  父行 `pid=NULL` 带 `word + description`，子行 `pid=父id` 存各同义词（`other_words`）。
- **匹配 + 注入**：`curd/terminology.py::select_terminology_by_word` 同样是
  `ILIKE` 子串 + embedding 双路；`get_terminology_template` 输出 `<terminologies>` XML
  注入 prompt。

## 四、区别与取舍

- **为什么 jieba 词元重叠起步，而不是 embedding**：SQLBot 面向多数据源、海量训练样本，
  必须上 pgvector。本项目 demo 示例只有几十条，词元重叠已足够且零基础设施依赖、结果可
  解释；二期示例上千后再换 `app/rag` 的向量化召回（`match_skills_by_query` 同样预留了
  这条升级路径）。同义词匹配也刻意用子串而非向量——术语是精确黑话，子串命中即够。
- **为什么一个合并工具而非两个**：术语和示例总是同时需要，合并成
  `sql_context_search` 一次取回，省一轮模型往返（少一次 LLM 调用、少一次工具轮次）。
- **为什么让模型主动调用，而不是 middleware 静默注入**：静默注入（SQLBot 的做法）省事，
  但把上下文藏进了 prompt，demo 时看不见、讲不清。做成显式工具调用后，每次命中的术语与
  示例都出现在工具轨迹里——**可观察、可讲解、可评估**，正是简历项目要展示的能力；也让
  「先取业务上下文再写 SQL」成为可被 eval 检查的显式步骤。
- **口径/示例仅作参考**：SKILL.md 明确要求表名/字段名仍以 `schema_search` 的真实
  M-Schema 为准（示例可能基于旧结构），避免示例过期误导生成。

## 五、文件清单

- `app/text2sql/examples.py` — ExampleStore（含 5 条种子；作用域去重 + verified-only 检索）
- `app/text2sql/terminology.py` — TermStore（含 GMV/复购率/客单价 种子；作用域命中）
- `app/text2sql/feedback.py` — SQL 执行记录器（ContextVar，对话回流的信号侧）
- `app/agents/tools/sql_context_tool.py` — 合并门控工具（作用域检索）
- `app/agents/tools/sql_tool.py` — execute_sql 成功路径 record
- `app/services/chat_service.py` — recorder 接线 + `sql_result` SSE 事件
- `app/api/routes_knowledge.py` — 示例库/术语库 CRUD API（datasource 归属校验 + source 白名单）
- `app/db/knowledge_migration.py` — 作用域列幂等窄迁移
- `evals/text2sql/export_failures.py` — 评测失败 → 候选示例（CLI 直写 DB）
- `evals/text2sql/compare_reports.py` — 两份报告 → Markdown 对比
- `app/core/dependencies.py` — 追加 `get_example_store/get_term_store` + gated_tools 注册
- `app/main.py` — 追加一行路由注册
- `app/skills/buildin/sql-generation/SKILL.md` — frontmatter 加 `sql_context_search` + 正文追加「④」小节
- `tests/test_sql_knowledge.py` — store/工具/API/SKILL 依赖展开 + 作用域隔离测试
- `tests/test_sql_feedback.py` — 记录器生命周期/多轮取最后成功/流式下发测试
- `tests/test_eval_export.py` — 评测回流与报告对比测试

设计文档：`docs/openspec/sql-knowledge-loop.md`。
