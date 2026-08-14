# 数据源接入与语义元数据审核闭环

## 1. 目标

把当前绑定 `data/ecommerce.db` 与外置注释字典的 Text-to-SQL 演示链路，升级为可由用户接入数据源的平台能力：

1. 管理员配置 SQLite、PostgreSQL 或 MySQL 数据源；
2. 系统只读扫描真实表、字段、主键、外键和数据库原生注释；
3. LLM 基于物理结构生成业务语义草稿，不读取业务数据样本；
4. 工作空间用户审核、修改后保存正式语义；
5. 只有已审核语义（以及数据库原生注释）进入 M-Schema，供 Text-to-SQL 使用；
6. 物理结构发生变化时，对受影响对象重新置为待审核，避免旧语义静默污染查询。

## 2. 正确边界

### 2.1 物理结构自动发现，不让用户手写字典

数据源同步通过数据库元数据接口发现表、视图、字段类型、是否为空、主键和外键。新增表后只需重新同步，无需修改 Python 字典。数据库已有的表/字段注释直接保留为物理注释。

### 2.2 AI 只产草稿，人工审核才生效

语义状态分为：

- `pending`：新发现、结构变更或已有 AI 草稿，尚未审核；
- `approved`：用户确认后的正式语义，可进入 Agent 上下文；
- `rejected`：用户明确拒绝 AI 草稿，Agent 仅使用数据库原生注释。

注释优先级固定为：`已审核业务语义 > 数据库原生注释 > 空`。AI 草稿不在生产优先级中，只能在审核预览中查看。

### 2.3 密钥与租户隔离

- API 不接收任意数据库 URL，而是接收结构化连接参数，服务端统一构造 URL；
- 远程数据库用户名和密码使用 Fernet 加密后落库，响应与日志永不返回明文；
- `DATASOURCE_SECRET_KEY` 未配置时拒绝创建远程数据源，不降级为明文保存；
- 数据源和语义元数据按 `workspace_id` 隔离；连接、删除和结构同步仅管理员可操作；
- 远程建连默认关闭，必须同时开启 `AUTH_ENABLED` 与 `DATASOURCE_REMOTE_ENABLED`；
- 远程连接默认要求 TLS；只有调用方显式传入 `ssl_mode=disable` 才会关闭；
- 数据库账号必须由部署方授予只读权限；应用层再叠加单语句、只读 AST、表字段与 LIMIT 校验。

SQLite 文件限定在专用的 `DATASOURCE_SQLITE_ROOT`（默认 `data/datasources`）下，与应用库隔离，
避免 API 借路径读取任意宿主机文件或把 `app.db` 当业务库查询。

## 3. 数据模型

- `data_sources`：工作空间、名称、类型、非敏感连接配置、加密凭证、同步状态和结构哈希；
- `data_source_tables`：物理表信息、物理/AI/已审核表注释、审核状态和结构签名；
- `data_source_columns`：字段类型与约束、物理/AI/已审核字段语义、同义词、外键关系、审核状态和结构签名。

同步在一个事务中 upsert 快照：未变化对象保留审核结果；新增或结构变化对象转为 `pending` 并清空旧 AI 草稿；已删除对象从当前目录移除。

## 4. API 工作流

```text
POST   /api/datasources                         连接测试 + 首次扫描 + 保存
GET    /api/datasources                         列出当前工作空间数据源
GET    /api/datasources/{id}                    查看连接摘要与同步状态
DELETE /api/datasources/{id}                    删除数据源及其元数据
POST   /api/datasources/{id}/sync               重新扫描物理结构
POST   /api/datasources/{id}/semantic-draft     AI 生成待审业务语义
GET    /api/datasources/{id}/metadata           获取结构、草稿和审核结果
PUT    /api/datasources/{id}/metadata/{table_id}/review
                                                   审核并保存一张表的正式语义
GET    /api/datasources/{id}/m-schema            预览实际注入 Agent 的 M-Schema
```

聊天请求可选携带 `datasource_id`。服务端把该选择与当前工作空间写入请求级上下文，`schema_search` 和 `execute_sql` 不向模型暴露数据源 ID，避免模型越权切换数据源。未选择时继续使用原有演示 SQLite，保证兼容。

旧术语库与历史 SQL 示例没有数据源维度，选中平台数据源时会停止注入这些全局兼容数据，避免跨库口径污染；后续再将两者升级为 `workspace_id + datasource_id` 作用域。

## 5. AI 草稿约束

- 每张表单独生成，控制上下文与失败边界；
- 单次最多处理 100 张表，并限制单表字段数、提示词和响应大小，避免异常 Schema 放大模型成本；
- 只发送结构、约束和原生注释，不发送业务行数据；
- 严格解析 JSON，只接收真实存在的表和字段；
- AI 只能补充表注释、字段注释和同义词，不能伪造字段或外键；
- 一批生成完全成功后再一次性保存，避免半批草稿。

## 6. 失败与恢复

- 首次连接或扫描失败：不创建数据源；
- 后续同步失败：保留上一次可用元数据，记录脱敏后的失败状态；
- AI 失败：不覆盖已有草稿或审核结果，可重试；
- 结构漂移：只让变化对象重新审核，未变化对象继续可用；
- Text-to-SQL 执行失败：错误回传模型自纠，不修改语义元数据。

## 7. 当前范围

本轮交付后端 API、持久化、SQLite/PostgreSQL/MySQL 连接器、请求级数据源选择、M-Schema/Text-to-SQL 接入、测试与操作文档。前端提供基础数据源选择和语义审核入口。当前数据源选择已接入同步/流式 Chat、同步 Analysis 和 ARQ Chat/Analysis 任务；评测仍走演示库。鉴权模式的前端登录/API Key 注入、复杂字段批量审批、权限角色细分和大规模 Schema 召回留作后续增强。
