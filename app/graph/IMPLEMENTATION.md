# 知识图谱实现说明（轻量平台化版）

本模块在 E 轮“三元组抽取 + SQLite + NetworkX”的基础上，增加了用户作用域、实体主表、
别名/属性合并、实体解析、Agent 路径查询和可选 Embedding 索引。它仍然是面向千级关系的
轻量图谱，不是 Neo4j/GraphRAG 的替代品。

## ① 目标与边界

- SQLite 是图谱事实与实体的持久化真相源，NetworkX 是作用域内的惰性查询镜像。
- 图谱可以绑定 workspace，也可以绑定已归属当前 workspace 的 datasource。
- `graph_search` 查询单实体邻居，`graph_path_search` 查询两个实体的确定性最短路径。
- Embedding 只增强实体解析；未配置模型、Milvus 不可用或向量失败时，仍回退精确名、别名和子串。
- LLM 抽取和 Schema 同步都是显式操作，不在文件上传时隐式触发。
- 不做 Neo4j、PPR、可变深度 Cypher、自动跨 workspace 合并或完整 GraphRAG chunk 重排。

## ② 数据与请求作用域

`app/graph/scope.py` 定义不可变 `GraphScope`：

```text
workspace_id: 所属 workspace
datasource_id: 可选数据源
scope_key: workspace:<id> 或 datasource:<id>
```

API 先从当前用户解析 workspace，再用 `DataSourceService.get_source` 校验 datasource 归属，
最后通过 `use_graph_scope` 写入请求级 ContextVar。Chat、Chat Stream、Analysis、ARQ worker
和 `/api/graph/*` 共用这条规则；Agent 工具不接收租户参数，避免模型伪造权限。未开启鉴权的
演示模式使用 `workspace:0`。

## ③ 存储模型与迁移

### 实体

`graph_entities` 保存稳定 ID、规范名、实体类型、别名、轻量属性和合并状态：

```text
id, scope_key, workspace_id, datasource_id
canonical_name, normalized_name, entity_type
aliases, attributes, status, merged_into_id
embedding_status, embedding_hash, created_at, updated_at
```

`graph_entity_aliases` 用 `(scope_key, normalized_alias)` 做精确别名索引，避免每次解析扫描
所有 JSON。属性冲突不覆盖旧值，而是保留带来源/优先级的多值结构。

### 关系

`graph_triples` 保留旧接口使用的 `subject/predicate/object/source`，并补充作用域、实体端点
ID、`source_type/source_ref/provenance/confidence`。`source` 继续兼容旧调用方，新的来源分类和
置信度用于溯源；默认值不会扩展旧接口返回。幂等范围为 `scope_key + (subject, predicate, object)`；实体
ID 作为合并和后续索引的稳定引用。旧版没有 `scope_key` 的表由
`app/db/graph_migration.py` 在 `init_db` 前迁移，原数据归入 `workspace:0`，不删除旧事实。

### 写入主链路

```text
手工三元组 / LLM 抽取 / 已审核 Schema
        → normalize_entity_name
        → GraphEntityRepository 实体合并（别名、属性）
        → GraphTripleRepository.add_many_with_entities（实体与关系同事务、作用域内幂等）
        → 按 scope_key 缓存的 NetworkX 镜像写后失效
```

首次启动且 `workspace:0` 为空时仍灌入 17 条演示种子；其他 workspace/datasource 不自动复制种子。

## ④ Schema 同步

`GraphService.sync_catalog` 接受数据源目录快照，把已审核或物理结构转换为：

```text
已审核目录
  → schema.table 实体
  → schema.table.column 实体
  → column -[属于]-> table
  → 真实 references -[引用]-> 目标字段
```

审核通过的 `reviewed_comment/reviewed_synonyms` 才作为业务属性；待审核 AI 草稿不会进入图谱。
入口是管理员显式调用的 `POST /api/graph/sync-catalog?datasource_id=...`，可重复执行。

## ⑤ Agent 与 API 查询

### 邻居查询

`graph_search(entity, depth)` 通过 `GraphService.query_entity` 在当前作用域的
`MultiDiGraph` 上运行 `ego_graph(undirected=True)`，返回真实方向的边和来源。

### 路径查询

`graph_path_search(from_entity, to_entity, max_hops=3)` 的主链路是：

```text
规范名精确命中
  → 别名精确命中
  → Embedding top-k（可选）/词法候选回退
  → 阈值与 Top1-Top2 margin 判断
  → NetworkX 无向最短路
  → 按真实边方向渲染谓词链
```

候选接近时返回 `ambiguous` 和候选列表，工具提示 Agent 先向用户确认；实体缺失返回
`missing`，实体已找到但路径超过上限/不可达返回 `unreachable`。路径默认 3 跳、工具最多 5 跳，
服务端同时限制 8 跳、64 个节点和 64 条边；当前只返回一条确定性最短路，因此没有额外的路径数参数。

## ⑥ Entity Embedding 与消歧

`app/graph/entity_index.py` 使用独立的 `graph_entities` Milvus collection（打开
`GRAPH_ENTITY_MILVUS_ENABLED=true` 才连接），实体文本由规范名、别名、类型和属性组成。
Milvus 可在重启后恢复向量；进程内向量缓存是小图谱和 Milvus 不可用时的快速回退。
`GRAPH_ENTITY_EMBEDDING_ENABLED=false` 时默认完全不发起实体向量请求。

`app/graph/resolver.py` 只在作用域内取候选，并遵守类型兼容、最小分数和领先 margin；任何
Embedding 异常都降级为词法匹配，不阻塞精确路径。实体向量成功写入进程内/Milvus 索引后，
服务回写 `embedding_status=synced` 和 `embedding_hash`；失败实体继续保持 `pending`，因而
关系写成功不依赖外部向量服务。进程内缓存采用 LRU 上限，避免实体持续增长导致无界占用。

管理员可通过 `POST /api/graph/entities/merge` 提交 survivor/duplicate。仓储在一个事务内合并
属性和别名、重指关系、先删后改去重冲突边，再把 duplicate 标记为 `merged` 并保留
`merged_into_id`，不物理删除审计对象。

## ⑦ 关键文件与接口

| 位置 | 责任 |
|---|---|
| `app/graph/scope.py` | 请求级 workspace/datasource 作用域 |
| `app/graph/entities.py` | 名称归一、属性合并、候选基础算法 |
| `app/graph/resolver.py` | 精确/别名/词法/Embedding 消歧 |
| `app/graph/entity_index.py` | 可选 Milvus 与进程内实体向量索引 |
| `app/graph/store.py` | 作用域三元组写入、NetworkX 镜像和实体入口 |
| `app/graph/service.py` | 抽取、目录同步、邻居/路径/合并门面 |
| `app/agents/tools/graph_tool.py` | `graph_search` / `graph_path_search` 文本工具 |
| `app/api/routes_graph.py` | 鉴权、数据源归属校验、作用域上下文和 API |
| `app/api/request_scope.py` / `app/datasources/context.py` | 统一数据源归属校验与 Agent 作用域上下文 |
| `app/db/graph_migration.py` | 旧 `graph_triples` 表兼容迁移 |

## ⑧ 测试与可讲边界

`tests/test_knowledge_graph.py` 覆盖抽取容错、幂等/重启、作用域隔离、实体属性合并与显式
合并、Embedding 回退、路径工具、API 和 Skill 门控；在仓库根目录使用
`.venv/bin/python -m pytest -q` 执行全量回归（Docker/Redis 不可用时外部用例按环境跳过）。
作用域缓存当前以顺序隔离/写后失效回归覆盖，没有额外编写多线程交错压测；并发安全依赖
按 `scope_key` 分桶和版本校验，后续若扩大图规模再补压力测试。

面试时应主动说明：NetworkX 节点当前使用可读规范名，实体 ID 主要用于持久化合并和向量主键；
图谱规模假设是千级，Milvus/Embedding 是可选增强，当前没有跨文档 GraphRAG 和 PPR。
