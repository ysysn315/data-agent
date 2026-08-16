# 图谱平台化设计与需求方案

## 1. 背景与当前边界

当前图谱由 `graph_triples`、NetworkX `MultiDiGraph`、LLM 三元组抽取和
`graph_search` 技能组成。它适合演示级口径溯源，但三元组是全局字符串数据，
还不能区分用户、工作空间和数据源；Agent 只能查询邻居子图，不能直接调用路径
查询；图谱也没有实体别名、属性和语义召回能力。

本轮继续沿用 SQLite + NetworkX 的轻量路线，不引入 Neo4j、PPR 或完整
GraphRAG。SQLite 是图谱真相源，NetworkX 只承担作用域内的查询镜像，Milvus
仅作为可选的实体 Embedding 索引。

## 2. 目标与非目标

### 2.1 目标

1. 图谱数据绑定到用户 workspace 或具体 datasource，并在 API、Chat、Stream、
   Analysis 和异步任务中保持作用域隔离。
2. Agent 通过 `graph_path_search` 直接调用确定性的路径查询，不需要生成 HTTP
   请求或 Cypher。
3. 对实体名称和别名提供 Embedding top-k 召回，Milvus 不可用时降级到精确名、
   别名和子串匹配。
4. 增加轻量实体模型，实现名称规范化、别名合并、属性多值合并和显式实体消歧。
5. 已审核的数据源 Schema 可以显式同步成表/字段实体和外键关系。

### 2.2 非目标

- 不在文档上传链路中自动触发 LLM 图谱抽取。
- 不引入 Neo4j、PPR、可变深度 Cypher 或完整 GraphRAG chunk 重排。
- 不把用户未审核的 AI 语义直接暴露给 Agent。
- 不把 `workspace_id`、`datasource_id` 暴露给 Agent 工具参数。
- Embedding 失败不能阻断 SQLite 图谱和精确路径查询。

## 3. 作用域设计

定义请求级 `GraphScope`：

```text
scope_type: workspace | datasource
workspace_id: 所属工作空间
datasource_id: 数据源作用域时填写
scope_key: workspace:<id> 或 datasource:<id>
```

规则：

- 选择数据源时使用 `datasource:<id>`。
- 未选择数据源但已登录时使用 `workspace:<workspace_id>`。
- 未开启鉴权的演示模式使用 `workspace:0`。
- 数据源作用域和 workspace 作用域默认严格隔离，不自动跨作用域合并。
- workspace 必须从当前用户解析，客户端不得提交任意 workspace ID。
- datasource 必须通过 `DataSourceService` 校验归属。

Chat、Chat Stream、Analysis、异步 Analysis 和 `/api/graph/*` 都必须设置同一个
请求级 ContextVar，避免图谱查询退回全局演示数据或发生并发串租户。

## 4. 数据模型

### 4.1 实体

新增 `graph_entities`：

```text
id, scope_key, workspace_id, datasource_id
canonical_name, normalized_name, entity_type
aliases, attributes, status, merged_into_id
embedding_status, embedding_hash, created_at, updated_at
```

`entity_type` 第一版只使用 `table / column / metric / concept / value / unknown`。
同一个 `scope_key + normalized_name` 默认只对应一个实体。实体 ID 是持久化合并和向量主键；
当前 NetworkX 镜像仍使用规范名称作为可读节点，接口输出无需额外做 ID→名称转换。

新增 `graph_entity_aliases`，对 `scope_key + normalized_alias` 建索引，避免每次
消歧都扫描所有 JSON 别名。

### 4.2 三元组

保留 `subject/predicate/object/source` 兼容旧接口，补充：

```text
scope_key, workspace_id, datasource_id
subject_entity_id, object_entity_id
source_type, source_ref, provenance, confidence
```

新的幂等范围为 `scope_key + subject + predicate + object`；实体端点 ID 同时写入，供
合并、追踪和向量索引使用。这样旧的字符串接口可以兼容，后续再切换到 ID 级边键。
旧数据迁移到 `workspace:0` 演示作用域，不能要求用户删除已有数据库。

## 5. 数据源 Schema 同步

增加显式的 datasource graph sync 能力：

```text
已审核 M-Schema
  → 表实体 schema.table
  → 字段实体 schema.table.column
  → reviewed_comment / reviewed_synonyms 属性
  → references 外键关系
```

只使用 `reviewed_comment`、`reviewed_synonyms` 和真实 `references`；`ai_comment`
和 `ai_synonyms` 在审核前不可被图谱查询看到。同步应是独立、可重试的管理操作，
不要阻塞语义审核接口。

## 6. Agent 路径查询

新增 Skill 门控工具：

```text
graph_path_search(from_entity, to_entity, max_hops=3)
```

工具直接调用 `GraphService`，不让模型生成 HTTP 或 Cypher。保留现有
`graph_search` 查询单实体邻居；两个实体关系、指标口径链和潜在 JOIN 路径使用
`graph_path_search`。

返回至少包括：输入实体、解析后的实体、匹配方式和分数、`found`、`hops`、节点
路径、真实方向的边、来源和可读 `chain`。`max_hops` 默认 3、上限 5，并限制最大
路径数、节点数和边数。

路径查询应先做实体解析：规范名 → 别名 → Embedding 候选 → 类型和阈值过滤，
解析结果为 `ambiguous` 时必须返回候选，Agent 不得自行猜测。

## 7. Entity Embedding 召回

新增独立 Milvus 集合 `graph_entities`，不要复用文档 `knowledge_base`：

```text
entity_id INT64（稳定主键，auto_id=False）
vector FLOAT_VECTOR
content VARCHAR
metadata JSON
```

metadata 至少包含 `scope_key`、`workspace_id`、`datasource_id`、实体名称和类型；
SQLite 实体表保存 `embedding_hash/embedding_status`。向量文本由规范名、别名、类型和审核
属性组成。

Embedding 召回是增强能力：

```text
精确规范名 → 别名 → Embedding top-k → scope 过滤 → 类型/阈值过滤
```

Embedding 未配置、Milvus 连接失败、维度不匹配或索引尚未恢复时，必须回退到
精确名、别名和子串匹配。实体写入 SQLite 成功不能因为向量写入失败而回滚。

支持惰性初始化、重启恢复和按实体 upsert；实体变化通过 `embedding_hash` 和
`embedding_status` 标记待更新状态，Milvus 失败时由进程内索引和按查询惰性计算兜底。

## 8. 轻量属性合并与实体消歧

### 8.1 规范化与候选

- Unicode NFKC、大小写、空白、连接符和常见标点归一。
- 作用域内规范名或别名精确命中时直接复用。
- 类型不兼容不自动合并。
- Embedding 只提供候选；只有分数超过可配置阈值且明显领先第二候选时才自动
  归一，否则返回 `ambiguous`。
- 同名实体在不同 workspace/datasource 内默认不合并。

### 8.2 属性策略

属性优先级：人工审核 > 物理 Schema > 手工录入 > LLM 抽取。

- 空值忽略，同值去重。
- 冲突值保留多值和来源，不直接覆盖。
- 审核值作为主值，LLM 值只作为低置信候选。
- 属性和关系都保留 `source_ref`、`confidence` 等 provenance。

### 8.3 显式合并

管理端提供 survivor/duplicate 合并操作。合并必须在事务中完成：合并别名和
属性、重指三元组端点、去重关系、标记 `merged_into_id`、更新 survivor 向量，
并保留审计信息。不能直接删除重复实体。

## 9. 实施顺序

1. 增加 GraphScope、请求上下文、API 归属校验和旧图谱兼容迁移。
2. 增加实体/别名模型和仓储，三元组改为作用域内幂等，保持旧接口输出兼容。
3. 增加 Agent `graph_path_search` 和路径边界，更新 knowledge-graph Skill。
4. 增加已审核 Schema 到图谱的显式同步。
5. 增加独立实体 Embedding 索引、降级、恢复和 reindex。
6. 增加属性合并、ambiguous 返回和显式 merge。
7. 补齐租户隔离、迁移、路径、Embedding 回退、合并和 API 鉴权测试。
8. 更新图谱实现说明、Schema 语义文档和 roadmap；通过测试后再提交 PR，不合并。

## 10. 验收标准

- workspace A、workspace B、datasource A、datasource B 的同名实体互不泄漏。
- Chat、Chat Stream、Analysis 和异步任务使用正确图谱作用域。
- Agent 能直接调用 `graph_path_search`，路径方向、谓词、来源稳定可解释。
- Embedding 能召回别名或语义相近实体；不可用时精确/别名查询仍可用。
- Top1/Top2 接近时返回候选而不是错误合并。
- 属性冲突保留多值和来源，审核值优先。
- 旧 17 条演示种子和现有图谱 API 仍可用。
- 数据库重启、向量索引恢复/失败回退和重复写入幂等。
- 现有 RAG、Text-to-SQL、Skills、MCP 和全量 pytest 回归通过。

## 11. 本分支落地映射

本分支已按上述边界落地：`GraphScope` 与旧表迁移、实体/别名仓储、Schema 目录同步、
`graph_path_search`、Embedding/Milvus 可选索引、属性合并与显式 merge，以及 Chat/Stream/
Analysis/ARQ 的作用域传递。Milvus 实体索引默认关闭；未部署 Milvus 时使用 SQLite + 进程内
向量/词法回退。未纳入本轮的是 Neo4j、PPR、跨文档 GraphRAG 和全量异步索引编排，后续若需
更大规模再单独设计。
