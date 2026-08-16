# 服务不可用（503）处置手册

## 判定

健康检查连续失败、或错误率超过 50%、或 P99 超过 3秒持续 5分钟，判定为服务不可用。

## 快速分诊（按概率排序）

> 数据源速查：application-logs（应用日志）、system-metrics（系统指标）、query_logs / slow_query（数据库日志）。错误率50%（超过一半请求失败）即达严重级。

1. **下游数据库**：system-metrics 看数据库连接池水位；slow_query 日志确认是否有锁等待/慢查询拖垮——数据库不可写入（磁盘满，见 disk_high_usage.md）会直接导致全部请求 500。
2. **应用自身**：application-logs 查 ERROR 集中出现的时间点与堆栈；OOM 重启（见 memory_high_usage.md）会造成周期性不可用。
3. **上游依赖**：依赖服务超时熔断，检查熔断器状态。

## 恢复顺序

先恢复可用再查根因：重启/扩容/降级摘流量，恢复后再按 application-logs + system-metrics 复盘。错误率 50% 以上时直接切流量到备用实例。

相关：术语词典类纯词面文档（monitoring_glossary_noise.md）不是处置依据；慢响应但未不可用见 slow_response.md；真实处置演练见 mock_incident_drill_script.md（注意：那是演练脚本，不是排障文档）。
