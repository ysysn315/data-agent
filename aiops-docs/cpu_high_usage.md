# CPU 使用率过高排查手册

## 告警阈值

CPU 使用率持续 5分钟超过 80% 触发告警（system-metrics 数据源，阈值 80%，窗口 5分钟）。

## 标准排查流程

1. **确认当前状态**：先查 system-metrics 拿当前时间的 CPU 指标，再用 query_logs 看同时段是否有异常慢查询——慢 SQL 消耗 CPU 是最常见根因。
2. **区分类型**：
   - us 高（用户态）：应用计算密集，查 query_logs 里的全表扫描/大聚合；
   - sy 高（内核态）：上下文切换或 IO 等待，结合磁盘指标判断；
   - wa 高（IO 等待）：不是 CPU 问题，转磁盘排查（见 disk_high_usage.md）。
3. **关联慢查询**：query_logs 中执行计划含全表扫描、无索引 JOIN 的语句优先处理。

## 处置动作

- 慢 SQL：加索引或改写 SQL；
- 突发流量：限流或扩容；
- 无法定位时采集 perf/火焰图，转性能团队。

注意：本手册只覆盖 CPU；内存问题见 memory_high_usage.md，整体响应慢见 slow_response.md。
