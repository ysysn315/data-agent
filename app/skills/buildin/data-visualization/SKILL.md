---
name: data visualization
slug: data-visualization
description: "将 SQL 查询结果生成可视化图表"
version: "1.0.0"
author: "data-agent"
dependencies:
  skills:
    - sql-generation
  mcps:
    - chart-mcp
---

# 数据可视化技能

将 SQL 查询结果转换为可视化图表，帮助用户直观理解数据。

## 操作流程

1. 接收 SQL 查询结果（DataFrame 或 JSON）
2. 分析数据特征（字段类型、数据分布、时间序列）
3. 推荐合适的图表类型（表格/柱状图/折线图/饼图/散点图）
4. 调用 chart-mcp 生成图表
5. 返回图表 URL + 图表说明

## 图表类型选择规则

- **表格**：默认，适合明细数据、多维度对比
- **柱状图（bar）**：分类对比，如"各品类销售额"
- **折线图（line）**：时间序列趋势，如"按月订单量变化"
- **饼图（pie）**：占比分析，如"支付方式分布"
- **散点图（scatter）**：相关性分析，如"价格 vs 销量"

## 约束

- 图表标题必须清晰说明数据内容
- 时间序列数据优先用折线图
- 分类超过 10 个时不用饼图（用柱状图）
- 必须说明图表的 X/Y 轴含义和单位

## 允许的工具

- chart_render: 渲染图表（调用 chart-mcp）
- recommend_chart_type: 推荐图表类型
- export_chart: 导出图表（PNG/SVG）