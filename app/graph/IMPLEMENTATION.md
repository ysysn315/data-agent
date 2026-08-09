# 知识图谱实现说明（app/graph，轻量版）

E 轮新增：LLM 三元组抽取 + SQLite 持久化 + NetworkX 内存图查询，
并以门控工具 `graph_search` 挂进 Skills 体系。对标 Yuxi 的完整图谱栈
（Neo4j+Milvus 双存储 + PPR 图推理），刻意做成演示级轻量版，取舍见 §4。

---

## ① 功能与用法

图谱数据是三元组：`主语 -[谓词]-> 宾语`（如 `GMV -[计算自]-> 订单项价格`），
存 `graph_triples` 表（subject/predicate/object/source/created_at，(s,p,o) 唯一）。
首次启动表空时自动写入 17 条演示种子（订单/客户/GMV/复购率/客单价 业务图谱），
来源标记 source 区分三类：`seed`（首启种子）/ `manual`（API 补录）/ `llm`（LLM 抽取）。

**使用路径一：HTTP API**（`app/api/routes_graph.py`，前缀 `/api/graph`）

| 端点 | 作用 |
|---|---|
| `POST /triples` | 手动补录三元组（幂等，返回 added/skipped/total） |
| `POST /extract` | body `{"text": ...}`，LLM 抽取三元组并入库，返回抽取结果 |
| `GET /entity/{name}?depth=1` | 实体邻居子图（出边入边都带谓词；不存在 → 404） |
| `GET /path?from=A&to=B` | 两实体最短路 + 谓词链（如 `GMV -[计算自]-> 订单项价格 -[属于]-> 订单项`） |
| `GET /stats` | 实体数 / 三元组数 / 谓词分布 / 来源分布 |

```bash
# 抽取（LLM 显式触发）
curl -X POST localhost:8000/api/graph/extract \
  -H 'Content-Type: application/json' -d '{"text": "发货时长计算自发货时间与下单时间"}'
# 口径溯源
curl 'localhost:8000/api/graph/path?from=GMV&to=客户'
# → {"found": true, "chain": "GMV -[按月分组于]-> 下单时间 -[属于]-> 订单 -[属于]-> 客户", ...}
```

**使用路径二：Agent 技能**（`app/skills/buildin/knowledge-graph/`）

内置技能 knowledge-graph 声明 `dependencies.tools: [graph_search]`，走标准三段式：
披露（system prompt 只见名称+描述）→ 模型 `read_skill("knowledge-graph")` 激活 →
门控工具 `graph_search(entity, depth=1)` 解锁。工具输出为逐行 `A -[谓词]-> B` 文本：

```
实体「GMV」的 1 跳邻居子图（2 条关系）：
GMV -[按月分组于]-> 下单时间
GMV -[计算自]-> 订单项价格
```

供模型做**指标口径溯源**（"GMV 怎么算出来的"）与**实体关联分析**（"订单和卖家
什么关系"）；实体名未精确命中时返回相近实体提示（"相近实体：客单价"），模型可
据此二次调用。`graph_search` 已挂进 `app/core/dependencies.get_chat_agent` 的
`gated_tools`，并由 Skills 中间件在 knowledge-graph 激活后解锁。

依赖注入：`app/core/dependencies.get_graph_service()` 单例；新依赖 `networkx>=3`
（pyproject「知识图谱」分组）。数据链路一览：

```
POST /extract ─ extractor.extract_triples(LLM)┐            ┌ query_entity（ego 子图）
POST /triples ─ 手动 dict ────────────────────┼→ GraphStore ┼ find_path（最短路+谓词链）
首启种子     ─ SEED_TRIPLES ─────────────────┘   ↓ ↑        └ stats
                                    graph_triples 表 ⇄ NetworkX 镜像（写后失效重建）
```

## ② 实现原理

**抽取（extractor.py）**：中文 prompt 要求「只输出 JSON 数组、主谓宾都是短名词/动词
短语、禁止编造（只抽文本明确表达的事实）、抽不出输出 []」。解析容错不引 json_repair：
先剥 ```` ```json ```` 栅栏，再「从每个 `[` 到最后一个 `]` 依次尝试 json.loads」——
前置杂文本里的中括号、对象包一层（`{"triples": [...]}`）都能穿透；全失败告警返回 []
（抽取是尽力而为，坏输出不炸调用方）。元素级校验：缺字段/空值/非对象跳过、批内去重。
空文本在 service 层短路，不消耗 LLM 调用；LLM 经 `llm_provider` 零参工厂惰性创建，
不调抽取就不要求 LLM_API_KEY（与 `app/core/llm.py` 的显式失败设计一致）。

**存储（store.py）——SQLite + NetworkX 双层**：
- 持久层 `GraphTripleRepository`（app/db/repositories.py）：幂等靠 add_many 的
  (s,p,o) 先查后插 + 表级唯一约束双保险；同步门面经 `run_sync` 后台循环桥接
  （与 TermStore/ExampleStore 同一套路）。
- 查询层 NetworkX `MultiDiGraph` 内存镜像：**惰性构建、写后失效**——首次读 `.graph`
  时从表全量重建；`add_triples` 有实际新增才把镜像置 None（幂等写不触发重建）。
  不做增量维护：演示级规模（千级三元组）全量重建毫秒级，而增量在并发/删除下
  极易出现镜像与表不一致的隐性 bug，简单正确优先。
  选 MultiDiGraph 且 edge key=谓词：同一对实体允许多条不同谓词的边，重建天然幂等。

**查询（service.py）**：两个原语都按**无向可达、有向呈现**——业务问句"A 和 B 有
什么关系"不关心边方向，但答案必须保留方向语义。`query_entity` 用
`nx.ego_graph(undirected=True)` 取 depth 跳内节点的诱导子图，边按真实三元组方向返回；
`find_path` 在无向视图上找最短路，逐跳回查真实方向渲染谓词链
（正向 `-[p]->`，反向 `<-[p]-`），edges 里永远存真实方向的 (s,p,o)。

## ③ Yuxi 是怎么做的（我们砍掉了什么）

参考 `/Users/ysn/projects/yuxi-reference/backend/package/yuxi/knowledge/graphs/`：

- **LLM 三元组抽取**（`extractors/llm.py` + `extractors/base.py` + `extractors/factory.py`）：
  prompt 要求实体带类型（label）与属性（attributes）的三层 JSON，`json_repair` 兜底修复，
  可选 schema 约束注入；`normalize_extraction_result` 做实体归一（`graph_utils.
  normalize_entity_name` 小写压空白）、同名实体属性合并、关系端点引用解析；
  factory 注册表支持多抽取器类型。→ 我们砍掉：实体类型/属性建模（只留扁平 s/p/o）、
  json_repair 依赖（自写括号截取重试）、抽取器工厂（只有一种抽取器时是多余抽象）。
- **Neo4j+Milvus 双存储**（`milvus_graph_service.py` + `milvus_graph_vector_store.py`）：
  图结构写 Neo4j（`graph_utils.py` 的 Cypher 模板：Chunk/Entity 节点、MENTIONS/RELATION
  边，全部 MERGE 幂等），实体与三元组文本另 embed 进 Milvus 每库两个 collection，
  用于"问句 → 语义召回种子实体"。→ 我们砍掉：整个双存储（SQLite 单表 + 内存图替代），
  以及 chunk 溯源建模（三元组只留 source 字符串标记来源）。
- **图谱推理**（`milvus_graph_service.py` 的 `query_seed_subgraph` +
  `query_and_rank_chunks_by_ppr` / `rank_chunks_by_ppr`）：Cypher 可变深度取种子子图，
  igraph `personalized_pagerank` 以种子实体为重启向量给 Chunk 节点打分，
  服务于 graph-RAG 的召回重排。→ 我们砍掉：PPR 重排与 graph-RAG 链路，
  收敛为 ego 子图 + 最短路两个确定性原语（演示场景可解释性 > 召回率）。

## ④ 取舍

**为什么 SQLite+NetworkX，不上 Neo4j**：演示图谱千级三元组，NetworkX 全内存足够
（ego/最短路都是毫秒级），Neo4j 带来的是一个常驻服务的运维成本（部署/备份/监控），
在本项目"SQLite 起步"的存储路线里不成比例。**升级路径已预留**：上层只依赖
GraphService 的五个方法（add_triples / extract_and_add / query_entity / find_path /
stats），换 Neo4j 时实现一个同签名的 Neo4jGraphService（Cypher 版 MERGE 幂等写 +
`apoc.path` 子图查询，参考 §3 的 Yuxi Cypher 模板）替换 `get_graph_service` 的构造
即可，路由/工具/技能零改动；`graph_triples` 表此时降级为导出源。

**为什么抽取不自动挂在文档上传链路**：LLM 抽取是按 token 计费的重操作，上传链路
自动触发意味着每份文档都隐性产生成本且失败难归因（Yuxi 也是独立的
`knowledge_graph_index` 任务而非上传即抽）。先做成 `POST /api/graph/extract`
显式触发：成本可控、坏输出可当场检视（响应里带抽取结果），等抽取质量与预算
稳定后，再由上层把它接进 D 轮的异步任务框架（app/tasks）跑批。

**为什么种子图谱对齐术语库**：`SEED_TRIPLES`（store.py）与
`app/text2sql/terminology.SEED_TERMS` 讲同一套口径——术语库回答"GMV **怎么算**"
（SUM(order_items.price)），图谱回答"GMV **沿什么链路算出来**"（GMV -[计算自]->
订单项价格 -[属于]-> 订单项）。两边实体名/字段中文名一致（订单项价格、客户唯一
标识、支付金额），模型同时调 `sql_context_search` 与 `graph_search` 时两份上下文
互相印证而不是互相打架；演示"口径溯源"时也能从图谱一路走到术语库的 SQL 口径。

**测试口径**（tests/test_knowledge_graph.py，全离线）：extractor 假 LLM 三态、
store 幂等/重启持久性/镜像失效重建、种子幂等、query_entity 深度与方向、
find_path 正反向谓词链、graph_search 文本化输出与相近实体提示、TestClient 走
全部端点（dependency_overrides 注入假 LLM）、SKILL.md 解析与依赖展开。
