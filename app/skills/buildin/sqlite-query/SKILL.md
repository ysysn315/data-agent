---
name: readonly sql query
slug: sqlite-query
description: "在请求选中的数据源上执行只读 SQL 查询并返回结果（兼容历史 slug）"
version: "1.1.0"
author: "data-agent"
dependencies:
  tools:
    - execute_sql
  skills:
    - sql-generation
---

# 只读 SQL 查询技能

在聊天请求选中的 SQLite/PostgreSQL/MySQL 数据源上执行只读查询；未选择数据源时使用演示 SQLite。
`sqlite-query` 是兼容既有依赖的历史 slug，不代表执行器只支持 SQLite。

## 操作流程

1. 先调用 `schema_search` 获取当前数据源的真实 M-Schema，不要依赖数据库私有元数据表。
2. 按 sql-generation 技能的规范生成 SELECT 语句
3. 调用 `execute_sql(sql, limit)` 执行，结果为 JSON（columns + rows）
4. 数据量大时先聚合再返回，不要拉全表

## 约束

- 仅允许单条 SELECT / WITH 查询，禁止任何写操作；应用层做 AST 校验，数据库账号还必须是只读账号
- 默认最多返回 100 行，上限 1000 行
- SQL 方言由当前数据源决定，不能把 SQLite 的 `date/strftime` 直接用于 PostgreSQL/MySQL

## 备选：脚本方式

本技能随附的 `scripts/query.py` 是**仅面向本地 SQLite 文件**的脚本示例；平台数据源统一使用
`execute_sql`，不要把远程数据库凭证传给脚本：

run_skill_script(slug="sqlite-query", script="query.py",
                 script_args=["--db", "<数据库路径>", "--sql", "SELECT ...", "--limit", "50"])
