# 持久化层实现说明（app/db）

SQLAlchemy 2.0 async 持久化层：应用库默认 SQLite，可切 PostgreSQL。最初承接
Skills/MCP/术语/示例，后续追加图谱、用户/工作空间和数据源语义目录。

---

## ① 功能与配置

当前表按领域分组如下：

| 表 | 领域对象 | 原存储 | 说明 |
|---|---|---|---|
| `skills` | `app/skills/models.Skill` | 内存 `InMemorySkillRepository` | 元数据索引，**正文不入库**（存 dir_path） |
| `mcp_servers` | `app/mcp/models.MCPServer` | `save_dir/mcp_servers.json` | 字段与 MCPServer 全字段一一对应 |
| `sql_examples` | dict | `save_dir/sql_examples.json` | question→SQL few-shot 示例库 |
| `terminology` | dict | `save_dir/terminology.json` | 业务术语库（term 唯一键） |
| `graph_triples` | 图谱三元组 | 内存图 | NetworkX 的持久化事实源 |
| `workspaces` / `users` | 工作空间与 API Key 用户 | 无 | workspace-lite 与鉴权 |
| `data_sources` | 用户数据源 | 固定 `SQLITE_DB_PATH` | 非敏感配置、加密凭证、同步状态 |
| `data_source_tables` / `data_source_columns` | 物理结构与语义 | 演示注释字典 | 物理/AI/人工三层元数据 |

**唯一配置项** `settings.database_url`（`app/core/settings.py`），默认：

```
sqlite+aiosqlite:///./data/app.db
```

**切 PostgreSQL 只改这一行**（.env 里的 `DATABASE_URL`），例如：

```
postgresql+asyncpg://user:pwd@localhost:5432/data_agent
```

其余代码零改动 —— 模型用的都是 SQLite/PG 通用的列类型（`JSON`/`DateTime`/`String`/`Text`），
仓储用的是纯 Core/ORM 语句，没有 SQLite 方言依赖。SQLite 首次启动会自动建 `./data` 目录与库文件。

建表：`app.db.ensure_initialized()` 幂等执行 `Base.metadata.create_all`。
入口有两处：`main.py` 的 startup 事件；以及 `dependencies.py` 里每个 DB 组件首次构建前
（保证测试等不走 startup 的路径也已建表）。

---

## ② 实现原理

### SQLAlchemy 2.0 Declarative（models.py）
用 `Mapped[...] / mapped_column(...)` 声明式建模，一个 `Base` 承载全部 metadata。
`skills` 表**刻意没有 content 列**（见第 ③ 节 Yuxi 思路）。

### async engine / session（engine.py）
- **单 engine 单 sessionmaker**，`settings.database_url` 驱动，`NullPool`。
  选 NullPool 是关键：连接不跨操作复用、每次操作现开现关，因此同一个 engine
  既能被 FastAPI 主事件循环用（技能仓储，天然 async），又能被后台事件循环用
  （下面的同步门面桥），不会踩「async 连接绑定单一事件循环」的坑。
- **sync→async 桥 `run_sync`**：MCPService / ExampleStore / TermStore 的公开方法是
  **同步**的（路由/工具直接 `store.add(...)`），但底层 DB 是 async。在 FastAPI 正在跑的
  主循环里 `asyncio.run` 会报 loop already running、submit 回主循环又自死锁。故起一个
  **独立守护线程 + 独立事件循环**承接这些同步门面的协程，`run_coroutine_threadsafe`
  提交并阻塞取结果 —— 跨循环所以不死锁。技能仓储是纯 async、由主循环 `await`，不走这条桥。

### repository 契约（repositories.py）
- `SqlAlchemySkillRepository` **实现与 `InMemorySkillRepository` 完全一致的方法集**
  （`get_by_id/get_by_slug/list_all/list_enabled/list_accessible_by_user/create/update/
  delete/enable/disable/exists/clear/count`）。`SkillService` 拿到哪个实现都无感 ——
  换的是底下的存储，不是上层 API。
- 采用 **autocommit-per-operation**（对齐 Yuxi）：每个方法自开 session、自提交，
  不把事务边界泄漏给上层。
- **正文永远从 `dir_path/SKILL.md` 现读**（`_read_content`），数据库不存正文。
- 数据源目录单独放在 `app/datasources/repository.py`。首次接入把数据源和完整快照放在
  同一事务；同步在一次事务中 upsert 表列、删除消失对象并更新结构哈希；一批 AI 草稿
  也在全部解析成功后一次提交，避免跨方法 autocommit 造成半批状态。

### JSON→DB 一次性迁移 & 种子幂等
三个同步门面只改了 `_load` / `_save` 两个私有方法（`if self._repo: ... else: <原 JSON 逻辑>`），
CRUD/检索逻辑一行没动，所以老的 JSON 路径（测试用）行为完全不变。DB 版加载分支：

1. 从 DB 读全量；**非空**直接用（这天然保证了幂等：迁移/种子只在表空时发生）。
2. 表空且存在历史 JSON 文件 → **一次性迁移**入库并 `logger.info`。
3. 表空且无历史 JSON → 写**种子**（示例库 5 条、术语库 3 条；MCP 无种子）。

写入用**整表替换**（`replace_all`：一个事务里清表 + 全量插入），语义对应 JSON 版的
「整文件原子重写」—— 注册表/示例/术语都是几条到几十条的量级，代价可忽略，换来的是与
原子文件写一致的心智模型。（技能仓储反过来用单条 autocommit，因为技能是逐条增删改的。）

---

## ③ Yuxi 怎么做的（参考，只读）

参考路径：`yuxi-reference/backend/package/yuxi/storage/postgres/`。

- **「内容存文件系统，索引存数据库」** —— `models_business.py:229-273` 的 `Skill` 表
  （类注释就写在 :230）。它有 `dir_path`（:244）、`content_hash`（:246）却**没有正文列**，
  正文与随附脚本都在文件系统，DB 只存元数据索引。本项目的 `skills` 表照搬这一思路：
  正文永远从 `dir_path/SKILL.md` 现读，这样渐进式披露的数据源单一化（只有文件系统一份），
  不存在「DB 正文」与「磁盘正文」不一致的问题。

- **会话管理** —— `manager.py` 的 `PostgresManager`：`create_async_engine(...)`
  （:55-64，带 `json_serializer=ensure_ascii=False`、`pool_pre_ping`、`pool_recycle`）
  + `async_sessionmaker(..., expire_on_commit=False)`（:66-71）+ `create_tables` 里
  `conn.run_sync(Base.metadata.create_all)`（:99-104）。本项目 engine.py 是它的收敛版：
  SQLite 起步不需要连接池调优，故用 NullPool；`expire_on_commit=False` 保留（提交后对象
  仍可读，避免多一次 IO）。Yuxi 还为 LangGraph 单开了一个 `autocommit=True` 的原生连接池
  （:80-85，注释「LangGraph Checkpoint 强依赖 autocommit」在 :85）—— 本项目暂无 checkpoint 需求，不引入。

- **autocommit-per-operation 的利弊** —— `agents/skills/repository.py`：仓储 `__init__(db_session)`
  收一个 `AsyncSession`（:10-12），每个写方法结尾 `self.db.add(item); await self.db.commit();
  await self.db.refresh(item)`（如 create :80-82）。**利**：调用点极简、无长事务、单条操作天然隔离；
  **弊**：跨多条的原子性要上层自己拼事务。本项目操作粒度都是单条（建/改一个技能、增删一条示例），
  正好吃到利、避开弊。一处差异：Yuxi 是「一个 session 注入进仓储、按请求作用域」，本项目仓储收的是
  `async_sessionmaker`、**每个方法开一个新 session**（因为同步门面经后台循环调用、没有请求作用域的
  session 可注入），粒度更细但语义一致。

> SQLModel（sqlbot-reference 的写法）也看过：它把 pydantic 模型和表模型合一、更省样板，
> 但本项目领域层已有成型的 pydantic（MCPServer）/dataclass（Skill）模型，再叠一层 SQLModel
> 反而要么侵入领域模型、要么双份定义，收益不抵改动，故不采用，保持领域模型与表模型解耦。

---

## ④ 取舍

- **为什么 SQLite 起步、不直接上 PG**：本项目是单机演示/面试形态，SQLite 零部署（一个文件），
  开发与 CI 都不用起数据库容器。代码用的是双通用列类型 + 纯 ORM 语句，`database_url` 一行切 PG，
  迁移成本已经前置消化。PG 的并发/MVCC 优势在当前写入量级（管理端偶发 CRUD）用不上。

- **为什么当前还没上 alembic**：起步阶段 schema 还在快速变动，`create_all` 幂等建表足够；此刻维护
  迁移脚本的成本大于收益。现在数据源目录已包含用户审核资产，部署到持久环境前必须引入 Alembic；
  `create_all` 只会创建新表，不会替已有表加列，不能作为线上升级机制。

- **为什么内置技能不入库**：内置技能是随代码发布的资产（`app/skills/buildin/` 下的目录），
  版本即代码版本，天然由 Git 管理。让它们入库反而要处理「代码更新了、库里是旧版」的同步问题。
  故 `SkillService` 启动时从文件系统把内置技能加载进内存缓存，**DB 只管 upload/remote 两类
  用户产生的技能**——这也和「正文存 FS」一脉相承：内置技能连元数据都不必进库。

- **为什么保留 InMemory / JSON 实现**：一是**测试**——大量现有用例直接 `InMemorySkillRepository()`
  / `ExampleStore(path)` / `MCPService(config_path=...)` 构造，纯内存/JSON 无 IO、无需建库、
  跑得快，且它就是仓储的接口契约参照；改用 DB 反而拖慢、引入偶发。二是**降级路径**——
  DB 不可用或纯离线场景仍可退回文件存储。所以 DB 版是**新增的存储后端**，不是删掉旧的，
  两者共存、由 `dependencies.py` 决定运行时注入哪个。
