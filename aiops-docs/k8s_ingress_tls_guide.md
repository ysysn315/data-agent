# K8s Ingress TLS 证书配置指南

## 场景

域名 HTTPS 接入 K8s 集群，通过 Ingress 挂载 TLS 证书。

## 步骤

1. **证书准备**：证书的 CN（Common Name）必须与访问域名一致；多域名用 SAN（Subject Alternative Name）扩展——现代浏览器只认 SAN，仅填 CN 会导致证书校验失败。
2. **创建 secret**：
   ```bash
   kubectl create secret tls my-tls --cert=tls.crt --key=tls.key -n ingress-nginx
   ```
3. **Ingress 注解**：spec.tls 引用上述 secret，hosts 与证书 SAN 列表保持一致。
4. **校验**：`openssl s_client -connect host:443 -servername 域名` 检查返回的证书链与 SAN。

## 常见错误

- CN/SAN 不匹配 → 浏览器报证书错误；
- secret 与 Ingress 不在同一 namespace → 挂载失败；
- 只配了证书没配 key 或反之。

相关：MySQL 的 TLS 与备份恢复（binlog/GTID/xtrabackup）见 mysql_backup_restore.md；WAF 对 HTTPS 流量的防护见 security_waf_playbook.md。
