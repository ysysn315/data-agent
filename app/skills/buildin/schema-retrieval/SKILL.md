---
name: schema retrieval
slug: schema-retrieval
description: "检索数据库表结构，根据用户问题找到相关的表和字段"
version: "1.0.0"
author: "data-agent"
---

# Schema 检索技能

根据用户的自然语言问题，检索数据库中相关的表结构信息，为 SQL 生成提供 schema 上下文。

## 操作流程

1. 理解用户问题，识别查询意图（查什么数据、什么维度、什么时间范围）
2. 将用户问题 embedding，与数据库表的 M-Schema 计算余弦相似度
3. 取 top-N 相关表（默认 N=5），返回表名、字段、注释
4. 如果涉及多表查询，补充表关系（外键关联、JOIN 路径）

## 输出格式（M-Schema）

```
# Table: ecommerce.orders, 订单表
[
(order_id:INTEGER, 订单ID),
(customer_id:INTEGER, 客户ID),
(order_status:VARCHAR, 订单状态),
(order_purchase_timestamp:TIMESTAMP, 下单时间)
]
```

## 约束

- 只返回查询相关的表，不返回全库 schema（避免 token 爆炸）
- 表注释必须来自数据字典，不编造
- 如果用户问题模糊，返回最可能的 3-5 张表，并说明选择理由
- 涉及多表时，必须说明表之间的关联关系

## 允许的工具

- schema_search: 检索表结构（embedding 相似度）
- get_table_relation: 获取表关系（外键、JOIN 路径）
- list_tables: 列出所有表（用于探索）