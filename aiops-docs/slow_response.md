# 慢响应（P99 超标）排查手册

## 判定

P99 延迟超过 3秒持续 5分钟告警；错误率超过 50% 或健康检查失败则升级为服务不可用（见 service_unavailable.md）。

## 分层排查（自上而下）

> 数据源速查：system-metrics（系统指标）、application-logs（应用日志）、query_logs / slow_query（数据库日志）。

1. **全局 vs 单接口**：system-metrics 先看是整体慢还是个别接口慢——整体慢优先查共享资源（数据库、连接池），个别慢查具体代码路径。
2. **数据库层**：query_logs / slow_query 日志找超过 1 秒的语句，按执行计划优化（同 cpu_high_usage.md 的慢查询处理）。
3. **应用层**：
   - GC 停顿：内存高（超过 85%，见 memory_high_usage.md）引发 Full GC，STW 直接体现为毛刺；
   - 锁竞争：线程 dump 看 BLOCKED；
   - 大对象序列化：单请求响应体过大。
4. **当前时间对照**：排查时先取当前时间的 system-metrics 基线，避免拿历史数据误判。

## 处置

慢 SQL 加缓存/索引；GC 问题按内存手册治本；突发流量限流。
