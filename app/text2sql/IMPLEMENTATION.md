# Text-to-SQL 核心（M-Schema + schema_search 门控工具）

> 路线图 P0-2（M-Schema 生成）/ P0-3（Text-to-SQL 提示词分层）。
> 定位：把"自然语言 → SQL"链路里最前置的一环——**让模型看到准确、带中文含义的表结构**——落地，
> 并把生成 SQL 的规则以 SKILL.md（提示词模板）而非硬编码的形式交付。

## 一、功能与用法

本模块交付三样东西：

1. **`app/text2sql/m_schema.py`**：M-Schema 生成器。输入 SQLite 库路径，输出 SQLBot 风格的
   表结构文本。核心函数：
   - `generate_m_schema(db_path, comments=None, tables=None) -> str`：整库（或指定表）的 M-Schema。
   - `build_table_m_schema(table_name, columns, table_comment, field_comments) -> str`：单表渲染（纯函数，好测）。
   - `list_tables(db_path) -> list[str]`：列出用户表。
2. **`app/text2sql/comments_ecommerce.py`**：演示库（Kaggle Brazilian E-Commerce / Olist）
   六张表 orders/order_items/customers/products/sellers/payments 的**表与字段中文注释字典**。
3. **`app/agents/tools/schema_tool.py`**：门控工具 `create_schema_search_tool(db_path)` →
   `schema_search(question: str) -> str`，返回全库 M-Schema，供 Agent 生成 SQL 前对齐字段。

典型调用（工具已通过 `schema-retrieval` 技能门控，激活后模型可见）：

```python
tool = create_schema_search_tool(settings.sqlite_db_path)
print(tool.invoke({"question": "查询各州的订单数"}))
```

输出示例：

```
# Table: orders, 订单表
[(order_id:TEXT, 订单ID), (customer_id:TEXT, 客户ID（关联 customers.customer_id）), (order_status:TEXT, 订单状态（delivered/shipped/canceled 等）), (order_purchase_timestamp:TEXT, 下单时间)]

# Table: order_items, 订单明细表（一个订单可含多个商品项）
[(order_id:TEXT, 订单ID（关联 orders.order_id）), (order_item_id:INTEGER, 订单内商品项序号), (product_id:TEXT, 商品ID（关联 products.product_id）), (seller_id:TEXT, 卖家ID（关联 sellers.seller_id）), (price:REAL, 商品单价), (freight_value:REAL, 运费)]
```

库文件不存在时，工具返回中文提示 `数据库文件不存在: <path>（请先导入演示数据集…）`，不抛异常。

## 二、实现原理

### M-Schema 格式

一张表渲染为两行：

```
# Table: <表名>, <表注释>
[(<字段>:<类型>, <字段注释>), (<字段>:<类型>), ...]
```

- 表/字段无注释时省略 `, 注释` 部分（见 `_column_entry` 与 `build_table_m_schema`）。
- 类型取自 `PRAGMA table_info` 的声明类型，统一大写；SQLite 时间列通常声明为 TEXT。
- 多表之间空行分隔，表按名排序，输出稳定（便于测试与缓存）。

### 注释字典（补 SQLite 无原生注释的坑）

SQLite 没有 `COMMENT ON`，`PRAGMA table_info` 读不到中文含义。于是把注释外置到
`comments_ecommerce.ECOMMERCE_COMMENTS`：`{表名: {"comment": 表注释, "fields": {字段: 注释}}}`。
生成器只在字典命中时输出注释，**未命中即留空、绝不编造**——这条不变量由
`test_generate_m_schema_no_fabricated_comment` 守护（库里的 `note` 列不在字典中，输出必须是 `(note:TEXT)`）。
字段名严格对齐 Olist 数据集真实列名（含官方拼写 `lenght`）。

### 门控工具如何接入 Skills 三段式

工具本身只读 SQLite（`mode=ro` URI），无副作用。它通过 Skills 系统的三段式对模型"按需可见"：

1. **披露**：system prompt 只列技能名称+描述（`schema-retrieval` 的 frontmatter）。
2. **激活**：模型调用 `read_skill("schema-retrieval")`，middleware 把 slug 写入 `state.activated_skills`。
3. **解锁**：`schema-retrieval` 的 `dependencies.tools: [schema_search]` 使该工具在下一轮对模型可见。

接线只在两处：
- `app/core/dependencies.py` 的 `get_chat_agent` 里把 `create_schema_search_tool(settings.sqlite_db_path)`
  追加进 `gated_tools`（构建期注册进 ToolNode，否则 langchain v1 执行时报 "not a valid tool"）。
- `schema-retrieval/SKILL.md` 的 frontmatter 声明 `dependencies.tools: [schema_search]`，正文写明用法。

`sql-generation` 依赖 `schema-retrieval`（skills 边），因此展开后 `schema_search` 也在其闭包内，
但只有**激活了 schema-retrieval** 才真正解锁（`ExpandedSkills.tools_of` 只认已激活技能的直接声明）。

## 三、SQLBot 是怎么做的

参考 `sqlbot-reference/`（只读）：

- **M-Schema 生成**：`backend/apps/datasource/crud/table.py:72-93`（`save_table_embedding`）。
  格式与本项目一致——`# Table: 表名, 注释` + `[ (字段:类型, 注释) ]`，
  且**注释为空即不输出**（`if field_comment == ''` 分支）。区别是 SQLBot 每字段单独一行（多行 M-Schema），
  注释来自入库时写进 `CoreField.custom_comment` 的元数据，而非外置字典。
- **schema 检索**：SQLBot 把每张表的 M-Schema 文本 `embed_query` 存进 `CoreTable.embedding`
  （同文件 95-99 行），查询时按**余弦相似度**召回 top-N 相关表，避免把整库 schema 塞进 prompt。
- **提示词分层**：`backend/templates/template.yaml` 把主模板（`system` 里的 `<Instruction>` +
  `process_check` 检查流程）、方言规则（`sql_examples/*.yaml`，如 `MySQL.yaml` 的
  `quot_rule`/`limit_rule`/`other_rule`）、零容忍规则（`query_limit` 的 `data-limit-policy`、
  `multi_table_condition` 的多表限定）拆成可组合的 YAML 片段，运行时按数据源方言拼装
  （`template.py` 的 `get_sql_template`）。本项目的 `sql-generation/SKILL.md` 正是把这套
  ①主规则 ②方言规则 ③零容忍规则的分层照搬进技能正文。

## 四、区别与取舍

| 点 | SQLBot | 本项目 | 理由 |
|---|---|---|---|
| schema 召回 | schema embedding + 余弦相似度 top-N | **全量注入**（6 张表） | demo 表少，全量 token 可控；embedding 需向量库+预计算，收益要表多才显现。二期换（工具 docstring 已注明） |
| 注释来源 | 入库元数据表 `CoreField.custom_comment` | **手工字典** `comments_ecommerce.py` | 无多数据源/同步链路，不值得为演示库建元数据表；字典改一行即生效 |
| 方言支持 | 12 种方言 YAML | 仅 SQLite | demo 单库；REQUIREMENTS §10 明确 3 种够用 |
| 提示词载体 | 后端 YAML 模板 + 运行时拼装 | **SKILL.md** | 技能即提示词模板：规则随技能披露/激活按需注入，改规则不改代码，正好体现 Skills 系统价值 |

**为什么全量注入不做 embedding 召回**：召回是为了在表多时省 token、提准确率；demo 六张表全量约几百 token，
召回的工程成本（向量库、预计算、相似度阈值调参）此刻是纯负担。把演进点写进 `schema_search` 的 docstring，
需求到了（表数上量）再切，不提前抽象。

**为什么注释用字典不建元数据表**：元数据表的价值在于多数据源、schema 自动同步、UI 可编辑；
本期都没有。一份 Python 字典就近维护、可 diff、可测，且天然是"宁缺毋造"的白名单。

**为什么提示词放 SKILL.md 而不是代码里**：这是本项目相对 my-agent 的关键差异——
Skills 系统让"能力=一段提示词模板+可选门控工具"。把 Text-to-SQL 的分层规则写进 `sql-generation/SKILL.md`，
它就享受渐进式披露（不激活不占 system prompt）、依赖展开（自动带出 schema-retrieval + schema_search）、
可被用户覆盖/远程安装等一整套机制。规则演进不触碰 Python 代码，正是 Skills 系统的价值所在。

## 五、遗留 / 二期

- schema_search 忽略 `question` 参数（全量返回）；表多时换 embedding 召回。
- 注释字典仅覆盖演示库六表；接入新数据源需另建注释来源（或元数据表）。
- 类型直接透传 SQLite 声明类型，未做跨方言类型归一。
