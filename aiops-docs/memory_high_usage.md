# 内存持续升高与 OOM 定位手册

## 告警阈值

内存使用率持续超过 85% 告警（system-metrics，阈值 85%）。

## 排查流程

1. **看趋势与 GC**：system-metrics 确认是缓涨（泄漏）还是台阶（大对象/缓存一次性加载）；Java 服务结合 GC 日志——Full GC 频繁且老年代不回落即泄漏特征。
2. **应用侧**：application-logs 查 OOM 前的最后日志；堆 dump 用 MAT 分析支配树，定位大对象持有者。
3. **常见根因**：
   - 缓存无上限：本地缓存/Map 只进不出；
   - 大结果集：一次查询拉全表进内存；
   - 资源未关：连接/流泄漏。

## OOM 处置

OOM Kill 后服务重启，先查 application-logs 的 OOM 记录与 dmesg 的 kill 事件，保留现场再重启。内存问题的连锁反应（GC 停顿导致慢响应）见 slow_response.md。
