---
name: sql generation
slug: sql-generation
description: "基于 schema 和业务上下文生成精准 SQL 查询"
version: "1.0.0"
author: "data-agent"
dependencies:
  skills:
    - schema-retrieval
---

# SQL 生成技能

基于检索到的表结构（schema）和业务上下文，生成准确、高效的 SQL 查询语句。

## 操作流程

1. 接收 schema-retrieval 提供的表结构（M-Schema 格式）
2. 理解用户查询需求，确定查询字段、过滤条件、聚合方式、排序、限制
3. 参考术语库和 SQL 示例（few-shot），生成符合方言规范的 SQL
4. 校验 SQL 语法（sqlglot 解析），确保可执行
5. 返回 SQL + 执行计划说明

## 生成规则（零容忍）

- 所有生成的 SQL 必须包含数据量限制（默认 LIMIT 1000，除非用户明确指定）
- 多表查询时，所有字段引用必须明确限定表名或表别名
- 只能生成查询语句（SELECT），禁止增删改（INSERT/UPDATE/DELETE/DROP）
- 不得编造 schema 中没有的表或字段
- 表名和字段名必须严格保持与 schema 一致（大小写、特殊字符）

## 方言规范

- MySQL：反引号 `` ` ``、LIMIT、CONCAT 百分比
- PostgreSQL：双引号 `"`、LIMIT、|| 字符串拼接
- SQLite：双引号 `"`、LIMIT、printf 格式化

## 允许的工具

- sql_validate: 校验 SQL 语法（sqlglot 解析）
- sql_execute: 执行 SQL 查询（只读）
- get_sql_examples: 获取相似 SQL 示例（few-shot）