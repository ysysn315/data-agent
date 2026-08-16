# Data Agent 面试防御手册

> 目标：围绕当前简历的 5 条主线讲清“问题、链路、设计原因、失败边界、证据”。本文只使用当前代码与可核验报告；旧 README/旧报告中的 92.86%、4 模型、113 tests 等数字已废弃。

## 0. 事实卡（2026-08-14）

| 主题 | 当前可核验事实 | 不要说 |
|---|---|---|
| 测试 | 提交前用 pytest/CI 验证；23 个 test_*.py 文件（含 conftest 共 24 个 Python 文件），Docker/Redis 不可用时外部集成用例按环境跳过 | 所有外部集成都已真机跑通 |
| Skills | 5 个内置技能；阶段测量约 1183→150 tokens/请求 | 当前 5 个技能仍精确等于早期测量口径 |
| Text-to-SQL | 支持 SQLite/PG/MySQL 接入、AI 语义草稿与人工审核；28 条演示集最高 89.29%（25/28） | 已有行列级权限或真实 PG/MySQL 集成环境全覆盖 |
| RAG | 主 Chat 已贯通 BM25 恢复、查询改写/扩展、RRF 与可选重排；独立实验可切换模型 | 本地 BGE 需额外依赖；BM25 是有上限的进程内派生索引 |
| 图谱 | workspace/datasource 作用域的 SQLite+NetworkX 轻量图谱；实体别名/消歧、路径工具、可选 Embedding、显式 LLM 抽取 | GraphRAG、Neo4j、大规模图推理 |
| 沙箱 | 默认 subprocess，可切 Docker 一次性容器 | 所有 Agent/工具默认运行在 Docker |
| 异步事件 | Streams 保存历史事件；SSE 可从头回放 | HTTP/前端已经支持断点续传 |
| Langfuse | 默认关闭，部分 Agent 图入口接 callbacks | 全链路可观测闭环 |

## 1. 项目总述

### 30 秒版本

> 我做了一个面向业务数据分析的 Agent 平台。用户可以接入自己的数据库，系统自动扫描物理结构，先由 AI 补充待审业务语义，再由用户审核后进入 M-Schema；中文问题随后经过业务上下文检索、SQL 生成校验、只读执行和结果解释。平台再用 Skills/MCP、RAG、轻量知识图谱和多步分析扩展复杂任务，并通过评测与回归约束效果。

### 3 分钟版本

按下面顺序展开，不要从框架名开始背：

1. **问数主链路**：管理员接入数据源并自动扫描 Schema，AI 只生成语义草稿，用户审核后才进入正式 M-Schema；问题再结合术语和 SQL 示例生成 SQL，由 sqlglot 按方言校验后通过只读连接执行。
2. **能力扩展**：Skills 只向模型披露名称和描述，模型读取正文后才激活并解锁工具；外部 MCP 工具也只在相关技能激活后加载。
3. **知识增强**：文档和表格进入 Milvus 知识库；知识库单例首次初始化恢复 BM25，主 Chat 统一执行查询改写/扩展、RRF 和重排；作用域图谱保存指标口径与实体关系，并由 Agent 路径工具查询。
4. **复杂任务**：Analysis Agent 进行规划、逐步执行和反思；长任务通过 ARQ、Redis Streams 和 SSE 异步推送进度。
5. **效果与可靠性**：工具层有超时、重试、熔断和降级；SQL 有双重只读；评测以结果集等价性衡量 28 条 SQL，用原始报告支撑 89.29% 最高准确率。

## 2. 五条简历主线

### 2.1 Agent 编排、MCP 与沙箱

**一句话**：Skills 把能力拆成按需披露、按需读取、激活后解锁三步，避免把所有技能正文和工具长期塞进模型上下文；MCP 与脚本执行沿同一激活边界扩展。

**主链路**：

```text
system prompt 披露技能名称/描述
  → 模型 read_skill(slug)
  → middleware 把 slug 写入 activated_skills
  → 下一轮解除本地工具隐藏
  → 按需加载该技能声明的 MCP 工具
  → ToolRuntime 执行并做超时/重试/熔断
```

**关键设计**：

- 本地门控工具必须在 Agent 构建期注册，运行时只隐藏模型可见性；否则模型即使生成调用，ToolNode 也无法执行。
- 动态 MCP 工具构建期不存在，因此请求期追加给模型后，执行阶段还要把真实工具实例覆盖回请求。
- 技能脚本先校验路径位于该技能的 `scripts/` 下，再交给 subprocess 或 Docker runner；远程技能默认禁用。

**边界**：Docker 模式是一项可切换能力，默认仍为 subprocess；当前容器还没有非 root 用户、capability drop、`no-new-privileges` 等生产级加固。

**高频追问**：

1. 为什么不把全部 SKILL.md 注入 system prompt？——正文长、技能增加时成本线性上涨；渐进披露让未使用技能不占正文 token。
2. 为什么不完全依赖 embedding 自动选择技能？——模型显式读取更可解释；语义匹配只是辅助，不能替代激活门控。
3. Docker 每次起容器是否很慢？——接受冷启动换隔离，适合低频脚本；高频任务可演进预热池，但当前没有这个需求。

### 2.2 Text-to-SQL 与业务语义

**一句话**：不是让模型裸生成、裸执行 SQL，而是用业务上下文、AST 校验和引擎级只读组成可自纠的执行链路。

**主链路**：

```text
中文问题
  → 请求级绑定当前工作空间数据源
  → schema_search 读取自动扫描且已审核的 M-Schema（当前全量返回所选 Schema）
  → 演示库可补充全局术语与 SQL 示例；平台数据源暂时禁用这两份未隔离数据
  → 分层规则生成 SQL
  → sqlglot 按 SQLite/PostgreSQL/MySQL 方言做 AST 校验/自动 LIMIT
  → 数据库只读连接执行
  → 结果解释
```

**关键设计**：

- 物理表列自动扫描；数据库原生注释直接保留，AI 只写待审草稿，只有 `approved` 人工语义进入正式 M-Schema。
- 远程用户名/密码用 Fernet 加密；`datasource_id` 由请求和工作空间绑定，不暴露给模型，避免模型枚举切库。
- AST 能识别 `WITH ... INSERT`、多语句和外层节点类型，字符串前缀/正则无法可靠保证只读。
- 非限定列只在能唯一确定归属时校验；多表、CTE、子查询宁可交给数据库报错，避免误报驱动模型反复改错。
- 校验错误作为中文 ToolMessage 回给模型，使 Agent 能继续自纠；真正异常才中断。

**边界**：SQLite 已有端到端测试，远程连接配置、加密和错误路径有自动化覆盖，但没有真实 PostgreSQL/MySQL 环境 smoke 证据；`schema_search(question)` 仍未按问题筛表，而是全量注入所选 Schema，术语/历史 SQL 也尚未按数据源建模（平台请求会禁用全局兼容库）。`datasource_id` 已贯通同步/流式 Chat、同步 Analysis 和 ARQ Chat/Analysis 的数据源与图谱作用域，但评测仍走演示库；此外还没有行列级权限、定时同步、凭证轮换和审批历史，不能包装成成熟企业 BI 平台。

**高频追问**：

1. 为什么比较执行结果而不是 SQL 文本？——正确 SQL 写法不唯一，业务目标是数据正确。
2. 多一个表需要手写吗？——不需要，同步会从数据库元数据自动发现；人工只审核业务语义，而不是抄 DDL。外置字典只保留给旧演示库兼容。
3. 为什么 AI 结果不能直接用？——Schema 名称真实不等于业务口径正确；草稿与正式语义分层，能阻断幻觉和错误口径静默进入 SQL。
4. AST 校验是否绝对安全？——不是；它负责语法和策略层，数据库只读账号/事务才是最终写保护。

### 2.3 RAG 与轻量知识图谱

**一句话**：RAG 负责从文档与表格召回非结构化知识，图谱负责显式保存实体关系与指标口径；两者互补但当前没有组成 GraphRAG。

**主应用 RAG 链路**：

```text
上传文件
  → 多格式解析/表格语义化
  → Markdown 标题分块或递归分块
  → Embedding
  → Milvus 写入
  → Agent 按需调用知识库工具
  → 稠密召回 + 元数据后过滤
```

**独立实验链路**：

```text
历史感知查询改写/扩展
  → Milvus + BM25
  → RRF 融合
  → BGE/LLM 重排
  → 去重与 RAG 生成
```

**关键设计**：

- CSV/Excel 不只按原始行存储，还转换成列说明、行级自然语言和统计摘要；Excel 保留 sheet 元数据。
- Markdown 按 H1/H2/H3 组织章节路径，其他格式走递归分块。
- 图谱使用 SQLite 保证作用域内实体/三元组持久化和幂等，NetworkX 提供轻量邻域/路径查询；LLM 抽取与已审核 Schema 同步由显式管理操作触发，避免上传即产生模型成本；实体解析按规范名/别名优先，可选 Embedding 召回，失败回退词法匹配。

**边界**：主 Chat 已统一接线，但 BM25 和文档列表都只处理最多 `RAG_BM25_MAX_DOCUMENTS` 个 chunk，重启后仍需从 Milvus 重建；Milvus 的恢复、检索、写入和列表扫描已放入工作线程，不占用事件循环；本地 BGE reranker 是可选依赖，未安装时回退配置的 LLM/融合排序。DOCX 只读普通段落，Markdown 超长章节不会二次按长度切分；图谱仍是千级轻量实现，没有 PPR 或跨文档 GraphRAG，实体向量索引也默认关闭。

**高频追问**：

1. 主 Chat 如何保证重启后仍能混合检索？——Milvus 保存 content/metadata，知识库单例首次初始化时由共享 VectorStore 在工作线程中分批恢复 BM25；BM25 是派生索引，超过上限时与增量写入一样保留扫描尾部的受控规模，稠密召回仍覆盖完整库。
2. 为什么还要图谱？——向量检索适合语义相关，图谱适合明确回答“实体之间是什么关系、指标由什么构成”。
3. 为什么不用 Neo4j？——当前是千级演示关系，SQLite+NetworkX 更轻；规模、并发和复杂图算法需求出现后再换。

### 2.4 多步分析、异步任务与追踪

**一句话**：简单问数走 Chat Agent；复杂分析先规划，再把每个步骤交回完整 Chat/Skills 工具链执行，最后反思缺口并生成报告。

**主链路**：

```text
Planner 生成步骤
  → Operation 逐步调用 Chat Agent
  → 收集真实工具轨迹与 SQL
  → Reflection 判断是否补一步
  → 限制补充次数防循环
  → Markdown 报告
```

长任务由 ARQ worker 执行；Redis Hash 保存状态，Streams 保存阶段事件，SSE 从头读取并推送。迟到订阅能看到历史事件，但断线后没有协议级游标续接。

Langfuse callbacks 默认关闭；同步/流式 Chat 以及 Analysis 的 Operation 步已有接入，Planner、Reflection、后台 Chat worker 等入口尚未全部覆盖。

**高频追问**：

1. 为什么用 ARQ 不用 Celery？——项目是 asyncio 栈，ARQ 可直接 await Agent/LLM/Redis，且只依赖已有 Redis。
2. 为什么 Streams 不用 pub/sub？——任务先执行、前端后订阅时仍要补看早期事件；pub/sub 不保留历史。
3. 如何防止 Reflection 无限追加？——状态中限制补充次数；回调失败也降级，不让观察能力拖垮分析。

### 2.5 评测与回归

**Text-to-SQL**：28 条分层用例覆盖单表聚合、多表 JOIN、TopN、时间过滤和 CTE。golden SQL 与模型 SQL 在同一库执行，比较归一化结果集。

可核验报告：

- qwen3.7-plus：25/28，89.29%；
- qwen3-coder-plus：24/28，85.71%；
- qwen3-coder-flash：23/28，82.14%。

**RAG**：40 条检索集 + 60 条分层生成模板，指标覆盖 Hit@K、Recall@K、MRR、NDCG、MAP、事实召回、来源精确率和幻觉惩罚。仓库里的 40 条检索 baseline 与 8 条生成 baseline 是旧链路产物，只作历史参照，不宣称当前优化提升。

**高频追问**：

1. 28 条会不会太少？——是演示级人工精标集，作用是建立可回归的最低基线，不代表生产泛化能力。
2. 如何处理 SQL 结果顺序？——golden 有外层 ORDER BY 时严格比较行序，否则按多重集合比较；TopN 并列仍可能暴露 tiebreaker 歧义。
3. 为什么 RAG 不写提升数字？——当前缺少与现行配置对应的完整 reports，只有历史 baseline，不能把两者混成对照实验。

## 3. 四个可讲的踩坑故事

讲法统一为：背景 → 现象 → 定位 → 修复 → 防回归。

### 3.1 工具“看得见”不等于“能执行”

门控工具如果只在请求期追加，模型能生成调用，但 ToolNode 在构建期不知道它，会报无效工具。修复是构建期注册所有本地门控工具、请求期只控制可见性；动态 MCP 工具则在执行钩子中覆盖真实实例。对应测试验证激活前隐藏、激活后可见且可执行。

### 3.2 CTE 与 SQL 只读判断

用首词判断 SQL 时，`WITH ... INSERT` 会被误当成查询。改用 sqlglot 解析外层 AST，只允许查询表达式；引擎同时用只读 URI 兜底。教训是安全策略不能依赖字符串外观。

### 3.3 初始化锁死锁

Chat Agent 初始化需要 SkillService，两者曾复用同一把不可重入的 `asyncio.Lock`，在锁内再次取依赖导致首次请求永久等待。修复是先在锁外解析依赖，再进入锁做双重检查，并补初始化路径测试。

### 3.4 Streams 有游标，不等于产品支持断点续传

底层 `read_events(after_seq)` 支持增量读取，但 SSE 路由没接收 `Last-Event-ID/after_seq`，帧也没发 `id:`，前端错误后主动关闭。文档一度把底层能力写成端到端能力。修正后只描述“历史事件回放”，也形成了文档必须验证实际路由和前端接线的教训。

## 4. 简历句与证据

| 简历主张 | 主要证据 | 必须主动说明的边界 |
|---|---|---|
| Skills 渐进披露、门控与 MCP 懒加载 | `app/skills/middleware.py`、`app/mcp/service.py`、中间件测试 | token 数是阶段测量 |
| Docker 一次性脚本沙箱 | `app/skills/sandbox.py`、沙箱测试 | 默认 subprocess；本轮真容器测试跳过 |
| M-Schema + sqlglot + 只读执行 | `app/text2sql/`、`app/agents/tools/sql_guard.py` | SQLite 演示级、列校验保守 |
| RAG 主线与实验链路 | `app/rag/`、`app/core/dependencies.py`、`evals/rag/` | 主 Chat 已统一组装；实验可独立切换配置 |
| 轻量知识图谱平台化 | `app/graph/`、`app/api/routes_graph.py`、`tests/test_knowledge_graph.py` | 非 GraphRAG/Neo4j；Milvus 实体索引默认关闭 |
| P-O-R + ARQ/Streams/SSE | `app/agents/analysis_agent.py`、`app/tasks/` | 历史回放，不是断点续传 |
| 89.29%（25/28） | `execution_qwen3.7-plus.json` | 3 份可区分报告、数据集较小 |

## 5. 面试前自检

每条主线都能脱离文档回答下面 5 个问题即可停止下钻：

1. 它解决什么业务或工程问题？
2. 输入、输出和 5～8 步主链路是什么？
3. 为什么选择当前方案而不是常见替代？
4. 哪个失败场景最危险，系统如何降级？
5. 哪个数字、测试或原始报告能证明它？

如果不能回答，回到对应 `IMPLEMENTATION*.md` 和 2～4 个关键代码位置定点补洞；不要为了记函数名逐行通读整个仓库。
