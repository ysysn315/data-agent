# Text-to-SQL：数据源目录、语义审核与只读执行

> 本文描述当前主线。`comments_ecommerce.py` 只服务未选择平台数据源时的演示库兼容路径，不再是新数据源的维护方式。

## 一、主链路

```text
管理员接入 SQLite / PostgreSQL / MySQL
  → 只读扫描真实表、字段、主键、外键和原生注释
  → AI 按表生成业务语义草稿（不读取业务行数据）
  → 工作空间用户审核、修改并保存
  → approved 语义 + 原生注释渲染 M-Schema
  → ChatRequest.datasource_id 固定请求级数据源
  → schema_search → sql_context_search → LLM 生成 SQL
  → sqlglot 按当前方言做单语句/只读/表列/LIMIT 校验
  → 只读连接执行 → Agent 解释结果
```

未传 `datasource_id` 时，`schema_search` 和 `execute_sql` 继续使用 `settings.sqlite_db_path`，保证原有演示和评测不受影响。

这里的 `schema_search` 目前是“按请求读取 Schema 目录”，还不是语义召回：参数保留了
`question`，但当前不会用它筛选表。演示库返回全部 6 张表；选择平台数据源后返回该数据源
当前 Schema 的全部表。表级 embedding/关键词召回、外键邻接扩展和 token 预算尚未实现。

## 二、数据源接入

### 连接参数与安全

API 只接收结构化参数，不接收可携带任意 driver/query 的数据库 URL。SQLite 路径必须位于 `DATASOURCE_SQLITE_ROOT`；PostgreSQL/MySQL 的用户名和密码由 `CredentialCipher` 使用 Fernet 加密后写入 `data_sources.encrypted_credentials`。未配置 `DATASOURCE_SECRET_KEY` 时，远程数据源创建直接失败，不会降级明文。

连接、同步和删除接口为 admin-only；目录、草稿和审核按 `workspace_id` 隔离。远程连接默认关闭且启用后默认要求 TLS，
只有同时启用 `AUTH_ENABLED` 与 `DATASOURCE_REMOTE_ENABLED` 才装配；生产部署还应给数据库账号授予只读权限，应用层校验不能代替数据库授权。

### 自动结构扫描

- SQLite：`sqlite_master` + `PRAGMA table_info/foreign_key_list`；
- PostgreSQL/MySQL：SQLAlchemy Inspector 读取表、视图、列、主键、外键和可用的原生注释；
- 每个数据源当前固定一个 schema，表名在该 schema 内唯一；
- 同步在一个事务中 upsert 快照，未变化对象保留审核结果，新增/变化对象重新置为 `pending`，已删除对象从当前目录移除。

因此新增一张表只需要重新同步，不需要再手写 Python 注释字典。

## 三、AI 草稿与人工审核

三层语义分开保存：

| 层 | 来源 | 是否进入正式 M-Schema |
|---|---|---|
| `physical_comment` | 数据库原生元数据 | 是，作为兜底 |
| `ai_comment/ai_synonyms` | LLM 草稿 | 否，只在审核预览中可见 |
| `reviewed_comment/reviewed_synonyms` | 用户确认结果 | 是，`approved` 时优先 |

正式优先级为 `approved 人工语义 > 数据库原生注释 > 空`。`rejected` 表示拒绝 AI 草稿，仍可使用原生注释。

AI 每张表单独调用，只发送表列、类型、主外键和原生注释，不发送业务数据样本；返回严格 JSON。服务会校验字段集合必须与真实结构完全一致，同义词数量和长度也有限制。一批表全部生成成功后才一次性保存，避免半批状态。
单次最多处理 100 张表，并限制单表字段数、提示词与响应大小；超大 Schema 需要通过 `table_ids` 分批提交。

人工批准一张表时必须逐字段确认，防止新字段在未被注意的情况下直接进入 Agent。结构签名变化后，受影响对象重新审核，旧审核文本保留但在 `pending` 状态下不生效。

## 四、请求级数据源选择

`ChatRequest` 新增可选 `datasource_id`。路由先校验它属于当前工作空间，再通过 `ContextVar` 写入请求上下文。两个工具的模型可见签名仍然只有：

```text
schema_search(question)
execute_sql(sql, limit)
```

数据源 ID 不暴露给模型，所以模型不能在工具参数中枚举或切换其它租户的数据源。请求结束后上下文立即 reset，并发请求彼此隔离。

现有术语库和历史 SQL 示例尚未包含 `workspace_id/datasource_id`，因此平台数据源请求会禁用这份全局兼容数据，只使用已审核 M-Schema；否则演示库口径可能串入用户数据库。数据源级术语与示例是后续独立建模项。

## 五、M-Schema

平台目录由 `app/datasources/m_schema.py` 渲染：

```text
# Table: orders, 已审核订单事实表
[(order_id:BIGINT, 订单ID；主键；同义词：订单号),
 (customer_id:BIGINT, 客户ID；关联 customers.customer_id)]
```

主键、真实外键和已审核同义词会拼入字段语义。`GET /api/datasources/{id}/m-schema?include_pending=true` 仅用于审核预览；Agent 运行时固定 `include_pending=false`。

`app/text2sql/m_schema.py` 与 `comments_ecommerce.py` 仍保留，作用仅是演示 SQLite 的兼容渲染和既有执行准确率评测。它们不会参与用户新接入数据源。

## 六、SQL 校验与执行

`validate_sql` 接收当前连接器方言（`sqlite/postgres/mysql`）：

1. sqlglot 解析并拒绝多语句；
2. 最外层必须是 SELECT/UNION，`WITH ... INSERT` 会被拒绝；
3. 根据同步目录校验真实表和能可靠定位的字段；
4. 外层无 LIMIT 时补 `LIMIT 1000`；
5. 连接器最多向工具返回 1000 行。

SQLite 使用 URI `mode=ro` 并用 progress handler 限时；PostgreSQL 在事务内设置 `READ ONLY` 与 statement timeout；MySQL 设置查询超时并要求部署方使用只读账号。错误转成模型可读文本供 Agent 自纠，不把凭证写入日志。

## 七、API 与前端

数据源管理页支持接入、同步、AI 草稿、逐表审核、正式 M-Schema 预览和删除；聊天页可选择数据源，切换时会清空当前会话，避免旧数据库上下文串入新查询。

完整 API 与状态机见 [`docs/openspec/datasource-semantic-metadata.md`](../../docs/openspec/datasource-semantic-metadata.md)。

## 八、当前边界

- `schema_search(question)` 当前未按问题筛表，而是全量注入一个 Schema 的 M-Schema；大库需要表级 embedding/关键词召回、关联表扩展和 token 预算；
- 请求级 `datasource_id` 已贯通 `/api/chat`、`/api/chat_stream`、同步 Analysis 和 ARQ Chat/Analysis 任务；执行准确率评测仍固定使用演示库；
- 没有行列级数据权限，租户隔离只覆盖数据源目录，数据面权限依赖只读数据库账号；
- 术语和历史 SQL 示例尚未按数据源建模，平台数据源请求当前会禁用全局兼容库；
- 没有凭证轮换 API、定时 schema 同步和审批历史表；
- 远程连接器有单元测试和装配测试，但需要在真实 PostgreSQL/MySQL 环境补 smoke test；
- 演示库外置字典是兼容资产，不应再扩展成多数据源方案。
