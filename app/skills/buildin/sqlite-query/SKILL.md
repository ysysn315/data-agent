---
name: sqlite query
slug: sqlite-query
description: "在演示电商数据库（SQLite）上执行只读 SQL 查询并返回结果"
version: "1.0.0"
author: "data-agent"
dependencies:
  tools:
    - execute_sql
  skills:
    - sql-generation
---

# SQLite 查询技能

在演示数据库（Kaggle Brazilian E-Commerce，SQLite）上执行只读查询。

## 操作流程

1. 先查表结构，确认表名和字段：
   `execute_sql("SELECT name, sql FROM sqlite_master WHERE type='table'")`
2. 按 sql-generation 技能的规范生成 SELECT 语句
3. 调用 `execute_sql(sql, limit)` 执行，结果为 JSON（columns + rows）
4. 数据量大时先聚合再返回，不要拉全表

## 约束

- 仅允许单条 SELECT / WITH 查询，禁止任何写操作（引擎级只读）
- 默认最多返回 100 行，上限 1000 行
- 时间字段是 TEXT（ISO 格式），比较时用字符串或 date() 函数

## 备选：脚本方式

本技能随附 scripts/query.py，可通过 run_skill_script 执行（效果同 execute_sql，
用于演示技能携带可执行脚本的能力）：

run_skill_script(slug="sqlite-query", script="query.py",
                 script_args=["--db", "<数据库路径>", "--sql", "SELECT ...", "--limit", "50"])
