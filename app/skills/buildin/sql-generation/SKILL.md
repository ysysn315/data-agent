---
name: sql generation
slug: sql-generation
description: "基于 schema 和业务上下文生成精准 SQL 查询"
version: "1.2.0"
author: "data-agent"
dependencies:
  skills:
    - schema-retrieval
  tools:
    - sql_context_search
---

# SQL 生成技能

基于 schema-retrieval 检索到的 M-Schema 表结构，生成准确、可执行的只读查询语句。
规则按 SQLBot 的分层思路组织：**① 主规则 → ② 方言规则 → ③ 零容忍规则**，逐层收紧。
（对齐 sqlbot-reference/backend/templates/template.yaml 的 system / generate_rules / query_limit 分层。）

## ① 主规则（Instruction & Process）

你的任务：根据用户问题和 M-Schema，生成一条符合当前数据源方言的 SELECT 查询。

生成流程（每一步都要执行）：

1. **先检索**：调用 schema-retrieval 技能的 `schema_search` 拿到真实表结构，
   确认要用的表名、字段名确实存在。**不允许在未检索的情况下凭记忆写表名/字段名。**
2. 分析用户问题，确定：查哪些字段、过滤条件、聚合方式、分组、排序、返回行数。
3. 依据 M-Schema 里的字段注释对齐"业务含义 → 字段名"（如"下单时间"→`order_purchase_timestamp`）。
4. 按当前数据源方言写 SQL；没有明确方言信息时使用 M-Schema 暴露的类型与常用 ANSI SQL。
5. **逐字复核**：SQL 里每个表名/字段名是否与 M-Schema 完全一致（拼写、大小写、下划线）；
   多表是否都加了别名；是否带了 LIMIT。任一不符则重写。

## ② 方言规则

- SQLite/PostgreSQL 标识符如需转义用双引号，MySQL 用反引号；字符串字面量统一用单引号。
- SQLite 演示库的时间字段是 TEXT（ISO8601 字符串，如 `2017-10-02 10:56:33`）。
  - 提取年月日用 `date(col)`；格式化用 `strftime('%Y-%m', col)`（如按月分组）。
  - **TEXT 时间比较**：ISO8601 文本可直接按字符串比较，如
    `order_purchase_timestamp >= '2017-01-01' AND order_purchase_timestamp < '2018-01-01'`。
    比较区间时优先用 `>= 下界 AND < 上界`，避免边界含糊。
- PostgreSQL/MySQL 的日期函数、字符串拼接和类型转换必须按各自方言生成，不能照搬 SQLite 函数。
- 三种已支持方言都使用 `LIMIT N`；聚合、`GROUP BY`、`HAVING` 和窗口函数按数据库版本能力使用。

## ③ 零容忍规则（违反必须重写）

- **默认 LIMIT 1000**：所有查询必须带行数限制。用户未指定数量时一律加 `LIMIT 1000`；
  用户说"所有/全部数据"也视为未指定，仍加 `LIMIT 1000`。用户明确指定"前 N 条"时用 N。
- **多表必须别名**：涉及多表（JOIN / 子查询）时，SELECT/WHERE/GROUP BY/HAVING/ORDER BY/ON
  中**所有**字段引用都要用表别名限定，即使字段名唯一也要限定。
- **只读**：只能生成 SELECT / WITH 查询，**禁止** INSERT/UPDATE/DELETE/DROP/ALTER/CREATE
  等任何写操作或 DDL（执行引擎也是只读，写操作会被直接拒绝）。
- **不编造**：绝不使用 M-Schema 中不存在的表或字段。拿不准时**先用 `schema_search` 确认**，
  而不是猜测。宁可少查也不虚构。
- **标识符原样**：无论回复用什么语言，SQL 中的表名/字段名必须与 M-Schema 逐字一致。

## ④ 生成前先取业务上下文（术语库 + 示例库）

在检索 schema 之后、动笔写 SQL 之前，调用 `sql_context_search(question)`，它一次返回：

- **命中的业务术语**：如"复购率""GMV""客单价"的**统一计算口径**。指标类问题必须按此口径算，
  不要自行发挥（例如"复购率"固定为「下单≥2 次的客户占比」，不要算成订单复购）。
- **相似历史 SQL 示例**：以往同类问题的 question→SQL，作为 few-shot 参考。
  借鉴其写法与字段用法，但要按当前问题调整过滤/聚合，**不要照抄**。

两段为空时说明库里没有相关沉淀，直接依据 M-Schema 生成即可。术语口径与示例仅作参考，
表名/字段名仍以 `schema_search` 返回的真实 M-Schema 为准（示例可能基于旧结构）。

## 交付

生成后返回：SQL 语句 + 一句话说明（查了什么、为什么这样过滤/聚合）。
如需执行，交给兼容 slug `sqlite-query` 的只读查询技能调用 `execute_sql`；工具会根据请求选中的数据源切换方言与连接。
