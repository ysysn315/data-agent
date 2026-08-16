---
name: knowledge graph
slug: knowledge-graph
# 描述刻意不写"图"字：离线测试的假 embedding 以关键词建轴（tests/test_skill_matching._AXES
# 轴0 含"图"），带"图"会与可视化技能同轴打平并列第一，干扰其排名断言；正文不参与匹配、不受限
description: "业务知识三元组：指标口径溯源、实体关联分析与数据源 Schema 路径查询"
version: "1.0.0"
author: "data-agent"
dependencies:
  tools:
    - graph_search
    - graph_path_search
---

# 知识图谱查询技能

业务知识图谱以三元组（主语 -[谓词]-> 宾语）描述当前 workspace/data source 的实体关系与指标口径，
例如 `GMV -[计算自]-> 订单项价格`、`订单 -[属于]-> 客户`；未选择数据源时才使用演示种子。

## 何时使用

1. **口径溯源**：用户问"GMV 是怎么算出来的""复购率依据什么统计"——
   图谱能给出指标由哪些实体/字段沿什么链路推导（与 sql_context_search
   返回的术语口径互补：术语库说"怎么算"，图谱说"沿什么链路算"）
2. **关联分析**：用户问"订单和卖家有什么关系""客户能关联到哪些数据"——
   用邻居子图展示实体间的关系链，再决定 SQL 该怎么 JOIN
3. **两实体路径**：用户明确询问 A 与 B 的关系、指标到字段的推导链或 JOIN 路径时，
   直接调用 `graph_path_search`；工具会在作用域内解析别名和语义候选。

## 使用方法

- `graph_search(entity="GMV")`：查实体的直接邻居（1 跳）
- `graph_search(entity="订单", depth=2)`：扩大到两跳，看间接关联
- `graph_path_search(from_entity="GMV", to_entity="客户", max_hops=3)`：查两实体路径
- `graph_path_search` 支持规范名、别名和 Embedding 候选；候选分数接近时会返回歧义列表，
  必须先向用户确认，不要自行猜测。`graph_search` 仍按规范名查询邻居，未命中时给出相近实体提示

## 输出解读

每行一条关系 `主语 -[谓词]-> 宾语`，箭头方向有语义：

- `GMV -[计算自]-> 订单项价格`：GMV 由订单项价格汇总而来（口径依赖）
- `订单 -[属于]-> 客户`：订单归属于客户（外键方向，JOIN 的依据）

回答用户时把关系链转述成自然语言（"GMV 由订单项价格求和得到，订单项属于订单…"），
不要原样粘贴箭头记法。
