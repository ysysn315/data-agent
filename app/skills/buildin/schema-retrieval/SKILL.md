---
name: schema retrieval
slug: schema-retrieval
description: "检索数据库表结构，根据用户问题找到相关的表和字段"
version: "1.1.0"
author: "data-agent"
dependencies:
  tools:
    - schema_search
---

# Schema 检索技能

根据用户的自然语言问题，检索数据库中相关的表结构信息（M-Schema 格式），
为 SQL 生成提供准确的表名、字段名与中文含义。

## 操作流程

1. 理解用户问题，识别查询意图（查什么数据、什么维度、什么时间范围）。
2. 调用 `schema_search(question=用户问题)` 获取表结构。
   - 当前演示库只有 6 张表，工具会**全量返回** M-Schema（表多时二期换 embedding 召回）。
3. 从返回的 M-Schema 中挑出与问题相关的表和字段，交给 sql-generation 技能生成 SQL。
4. 涉及多表时，依据字段注释里的关联说明（如"关联 orders.order_id"）确定 JOIN 路径。

## M-Schema 格式说明

`schema_search` 返回的每张表形如：

```
# Table: orders, 订单表
[(order_id:TEXT, 订单ID), (customer_id:TEXT, 客户ID（关联 customers.customer_id）), (order_status:TEXT, 订单状态)]
```

- 首行 `# Table: 表名, 表注释`（无注释则省略逗号后的部分）。
- 方括号内每项为 `(字段名:类型, 字段注释)`；无注释时为 `(字段名:类型)`。
- 类型是 SQLite 声明类型（时间字段通常是 TEXT）。

## 约束

- 表名、字段名必须**逐字**取自 `schema_search` 的返回，不得编造或改写（大小写、拼写完全一致）。
- 只把与问题相关的表交给下游，减少无关上下文。
- 字段含义以 M-Schema 里的注释为准；注释缺失的字段不要臆测其业务含义。
- 若 `schema_search` 提示"数据库文件不存在"，说明演示数据集未导入，应如实告知用户而非编造表结构。
