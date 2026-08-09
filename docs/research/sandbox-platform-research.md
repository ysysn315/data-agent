# 生产级沙箱平台调研报告

> 调研对象：业界某生产级 AI 沙箱基础设施（内部技术文档，已脱敏）
> 调研日期：2026-08-03
> 调研目的：评估该沙箱平台的核心技术点，对标 data-agent 现有 `app/skills/sandbox.py`，提炼可借鉴的改进方向
> 性质：只读调研，未修改任何外部文档。本文档已脱敏，仅保留可复用的技术思路

---

## 一、概述

**调研对象是业界某公司基于自研算力平台的通用 AI 沙箱基础设施**，为 Agent / 训练 / Computer Use 场景
提供按需、快速、隔离的代码执行环境。支撑规模约 **17 万核、3 万沙盒/分钟**，已发布多个版本。

定位上，该平台解决的是「通用 AI 工具能写代码但跑不完安全闭环」的问题——把不可信代码关进双层牢笼
（内核隔离 + 网络隔离），同时通过预热池把冷启动成本摊薄到亚秒级。它是**生产级、超大规模**的沙箱平台，
与 data-agent 的本地 Docker 一次性容器不在一个量级，但技术思路逐点可对照，对 data-agent 的沙箱演进
有明确的参考价值。

**生态背景**：兼容 E2B 协议（业界沙箱 API 标准），曾基于 E2B SDK，后迁移至自研 SDK。底层调度复用
该公司自研算力平台。

---

## 二、系统架构（四层）

| 层 | 组件 | 职责 | 关键技术 |
|---|---|---|---|
| **接入层** | gateway | 协议适配、鉴权、限流、配额预检 | 双认证中间件、三级配额预检 |
| **预热池层** | OpenSandbox Operator (k8s 1.32, 专有场景) + WarmPool (k8s 1.16, 通用, 复用自研调度) | 预热容器，亚秒级分配 | BatchSandbox CRD、Pool 三层分支、DB 缓存 + CRD source of truth |
| **镜像层** | image 服务 | OCI→Nydus 转换 + LRU 分片预热 | Nydus 懒加载、nydus-converter 异步任务 |
| **管控层** | Admin / 租户控制台 | 模板 / 配额 / 灰度 / 审计 / 运维大盘 | 四层大盘、Primary-Canary 灰度、Leader 选主 |

**关键架构决策**：没有套用通用开源框架，基于原生能力自建定制化执行引擎；选择**单控制面 + 预热池**
架构（而非 OpenKruise Agent），原因是 OKA 有两个致命问题——MutatingAdmissionWebhook 全局拦截
（故障会拖垮整个 K8s 集群）和 O(N) etcd 写放大（3 万/min ≈ 15 万次 etcd 操作）。OpenSandbox 的
优势是 O(1) 控制面（BatchSandbox CRD 整批仅 3 次 etcd 写）、Egress Sidecar 运行时独立、控制面
独立于 K8s、无 Webhook。

---

## 三、核心技术点

### 3.1 隔离模型：双层牢笼

**内核层 — MicroVM 隔离**
- 采用 Kata / Firecracker MicroVM，VM 级内核隔离
- 即使容器内拿到 root，也不破坏宿主机边界——比普通 Docker 容器隔离强一档
- 适合不可信代码执行场景（Agent 生成的代码、用户上传脚本）

**网络层 — Egress Sidecar + mitmproxy**
- 主容器 `--network=container:{sidecar}` 共享 sidecar 网络命名空间
- **`cap_drop NET_ADMIN`**——主容器无法改路由/iptables，物理上不可绕过代理
- 生成 `egress_token` 写入 Docker labels 作身份校验
- K8s RuntimeProvider 映射为 NetworkPolicy：仅放行公网 IP，阻断 RFC1918 私有段和云元数据服务
- 控制粒度精确到 URL，故障半径仅单沙箱，即时生效

**对比的另外两个方案（均被放弃）**：
- 防火墙白名单（SNI 域名放行）：30min 生效、新机器同步慢
- Pod SNAT/Masquerade（ip-masq-agent DaemonSet）：宿主机粒度、需改 CNI/DaemonSet、主容器有 NET_ADMIN 可绕过

### 3.2 启动加速：把冷启动成本摊到预热池

- **Nydus 懒加载**：1.3G 镜像冷启动 ~1.3s（pull 587ms + create/start 672ms），只读 6~10% 数据
- **预热池三层分支**：
  - 命中 + 空闲 → O(1) 返回（<200ms）
  - 命中 + 无空闲 → 阻塞等待
  - 未命中 → NonPooled 直接创建
- **CRIU 进程级快照**：内存/FD/连接快照，fork 比重建快 100x
- **镜像预热任务**：批量提交 OCI 镜像转 Nydus（RAFS v6），异步轮询，支持 degraded 降级回退原始镜像

### 3.3 控制面：O(1) 避热点

- **BatchSandbox CRD**：整批仅 3 次 etcd 写，支持 3 万/min 批量创建
- controller DB 作调度缓存，Pool CRD 为 source of truth，异步同步
- 多集群 kubeconfig 直连 + Watch 熔断（断连即拒绝调度，DB 标过期）
- 生命周期 GC：Deadline 回收 + Idle 归还 + 残留孤儿清理

### 3.4 生命周期与执行协议

**状态机**：
```
STARTING → RUNNING → DESTROYED
              ↘ FAILED (任意阶段都可能失败)
```
- 只有 probe 健康检查通过后实例才标记为 RUNNING
- probe 配置：httpGet + initialDelayMs + probePeriodMs + failureThreshold

**envd gRPC 协议**：
- 沙箱内守护进程，提供 Exec / PTY / 文件 / 进程接口
- 端口 <4000，零静态依赖
- 沙箱创建后返回 `envdAccessToken`，Agent 直连 envd 执行代码（不经 controller）

**E2B 协议兼容**：
- `POST /sandboxes`（HTTP 201）创建
- `/:sandboxID/connect` 连接
- `DELETE /:sandboxID`（HTTP 204）销毁
- 让 E2B SDK 直接可用，不走统一 `{code,msg,data}` 包络

**超时与回收**：
- 创建时指定 `timeout`（最小 30s，最大 24h）
- 超时 GC 协程 30s 扫描自动销毁
- Agent 需 `POST /sandboxes/{id}/timeout` keepalive 续时，否则沙箱被回收

### 3.5 鉴权与多租户

**双认证中间件**（同一套接口自动识别）：
- SSO：个人，工号 → 租户映射表 → tenantId
- Access Token / Bearer：服务，服务标识 → 直接关联 tenantId
- 管理接口仅 SSO + 白名单（配置中心热更新，不重启）

**三级配额**：
- 配额池（全局）→ 结算单元（成本中心）→ 租户（个人 to-C 自动创建 / 服务 to-B 绑服务标识）
- 支持继承语义、扩缩容校验防下溢
- **两阶段配额分配**消除 TOCTOU 竞态（沙箱平台最典型 bug）
- 规格预设四档：1C2G / 2C4G / 4C8G / 8C16G，自定义上限 32 核 / 64GB

### 3.6 运维运营

- **四层大盘**：实时 30s 轮询 / 准实时 5min 聚合 / 统计 1h / 报表 T+1
- **灰度发布**：Primary vs Canary 指标对比（启动成功率 / 耗时 / OOM / 异常退出），超阈值告警
- **Leader 选主** + 强制选主保单点高可用
- 监控埋点覆盖核心 API，支持告警配置
- 写操作全审计、二次确认；admin / viewer 两级权限

### 3.7 SDK 接入模式

完整调用链路：

```
申请服务标识 → 管理员建租户配额（挂结算单元）→ 建资源类型 / 模板 ID
    ↓
Client.builder().host().secretKey().build()
    ↓
createTemplate（镜像 + 资源 + 网络 + command + env + probe + ports 打包成模板）
    ↓
startSandbox（模板 ID + timeout + customConfiguration + metadata + clientToken 幂等）
    ↓ 返回 instanceId + endpoint + accessToken + envdAccessToken
轮询到 RUNNING
    ↓
envdAccessToken 直连 envd 执行代码（不经 controller）
    ↓
周期 keepalive 续时
    ↓
stopSandbox 释放配额
```

**幂等性**：`clientToken`（≤64 字符）防止重复提交。

---

## 四、与 data-agent sandbox 的对比

### 4.1 设计思路对照

data-agent 现有沙箱实现见 `app/skills/sandbox.py` + `app/skills/IMPLEMENTATION-sandbox.md`：
`ScriptRunner` Protocol + `SubprocessRunner`（默认无隔离）+ `DockerRunner`（一次性容器）。

| 维度 | data-agent sandbox.py | 调研平台 | 对照结论 |
|---|---|---|---|
| 隔离方式 | Docker 容器（共享内核） | MicroVM（Kata/Firecracker，独立内核） | 量级差异；data-agent 威胁模型是技能脚本非恶意逃逸，Docker 足够 |
| 网络隔离 | `--network none` 全断网 | Egress Sidecar + mitmproxy，URL 粒度 | 思路一致；Sidecar 是加强版，全断网是正确默认 |
| 文件系统 | `--read-only` + `--tmpfs /tmp` | MicroVM 只读根 + tmpfs | ✅ 完全一致 |
| 资源限额 | `--memory 256m` `--cpus 0.5` `--pids-limit 64` | 四档规格 + 自定义上限 | 思路一致；data-agent 单一规格 |
| 生命周期 | `--rm` 一次性 + 超时 `docker rm -f` 兜底 | TTL GC 30s 扫描 + 强制回收 + 安全删除 | ✅ 一次性 + 兜底回收思路一致 |
| 协议抽象 | `ScriptRunner` Protocol（多后端可切） | E2B 协议兼容（多后端） | ✅ 协议抽象一致 |
| 故障处理 | `SandboxUnavailableError` 降级文案 | 故障隔离、降级 | ✅ 故障不炸主流程一致 |
| 预热 | 无（冷启动） | 预热池 + Nydus 懒加载 + CRIU | 量级差异；data-agent 单机低并发不需要 |
| 配额 | 无 | 三级配额 + 服务标识粒度 max_concurrent | data-agent 缺并发控制 |
| 鉴权 | `auth.py` 工作空间（API Key） | 双认证 + SSO + 配置热切换 | data-agent 简化版 |
| 多租户 | 工作空间隔离 | tenantId + billingUnitId 两级 | data-agent 简化版 |

**核心结论**：data-agent sandbox.py 的设计思路（一次性容器 + 断网 + 只读 + 资源限额 + 超时回收 +
协议抽象 + 故障降级）**全部被生产级印证**。该平台用 MicroVM + Sidecar + 预热池把每项做到更强，但
底层逻辑同构。data-agent 作为个人项目，Docker 一次性容器是正确的量级选择——REQUIREMENTS §9
「沙箱太重，先 subprocess，二期容器化」的决策与该平台「工具先行」方法论一致。

### 4.2 量级差异（不可逾越，也无需逾越）

| 指标 | data-agent | 调研平台 |
|---|---|---|
| 并发量级 | 单机、低并发（技能脚本） | 3 万沙盒/min、17 万核 |
| 启动延迟 | Docker 冷启动 ~1-2s | 预热池 <200ms |
| 隔离强度 | 容器级 | VM 级 |
| 部署形态 | 单机 colima/Docker | 多集群 k8s + 预热池 |

---

## 五、可借鉴的改进点

按「价值 / 成本」排序，区分**可落地**与**不适用**。

### 5.1 可落地（推荐排期）

#### 改进 1：幂等 token 防重复执行 ⭐ 最高价值

- **调研平台做法**：`clientToken` 防重复提交
- **data-agent 痛点**：`tool_runtime.py` 有重试机制，重试时 `DockerRunner.run` 会重复执行脚本
  （`uuid.uuid4().hex` 容器名每次不同，无法去重）。一次网络抖动可能导致脚本执行两次，对有副作用的
  脚本（写文件、调外部 API）是真实风险
- **落地方案**：`run()` 接收 `idempotency_key`，基于 key + script 路径做去重缓存（TTL 等于脚本超时
  时间），重试时命中缓存直接返回上次结果
- **对接点**：现有 `TOOL_POLICIES` 重试链路
- **成本**：低（缓存层 + key 透传，不引入新依赖）

#### 改进 2：资源规格预设档 ⭐ 低成本

- **调研平台做法**：四档 1C2G / 2C4G / 4C8G / 8C16G
- **data-agent 现状**：单一 256m / 0.5cpu，所有脚本同等规格
- **落地方案**：Settings 加 `skill_sandbox_profile: lite|standard|heavy`，技能 SKILL.md 声明所需
  档位（lite=256m/0.5cpu 轻脚本，standard=1g/1cpu 数据处理，heavy=2g/2cpu 重计算）
- **对接点**：已有 `skill_sandbox_image/memory/cpus` 配置，只需预设组合 + SKILL.md 声明字段
- **成本**：低

#### 改进 3：工作空间并发配额 ⭐ 防资源耗尽

- **调研平台做法**：服务标识粒度 `max_concurrent`，三级配额
- **data-agent 痛点**：有 `auth.py` 工作空间，但无沙箱并发限制——多技能并行执行（analysis_agent
  P-O-R 各步可能并发）时可能耗尽本机内存
- **落地方案**：工作空间维度加 `max_concurrent_sandboxes`（默认 2），`DockerRunner` 执行前检查
  在途容器数，超限返回降级文案（复用 `SandboxUnavailableError` 模式）
- **对接点**：现有 `auth.py` 工作空间 + `SandboxUnavailableError` 降级机制
- **成本**：低（信号量 + 工作空间维度计数）

### 5.2 可选（场景受限）

#### probe 健康检查（仅长驻服务脚本）
- **调研平台做法**：httpGet probe + initialDelay + failureThreshold，通过才 RUNNING
- **data-agent 现状**：`create_subprocess_exec` 后直接 `communicate`，无「就绪」概念
- **适用场景**：仅当技能脚本起 http.server 等长驻服务时才有用；多数脚本是「跑完即退」，价值有限
- **建议**：暂缓，仅当支持长驻服务脚本时再排期

#### Egress 受限出网（轻量版）
- **调研平台做法**：Sidecar + mitmproxy，URL 粒度
- **data-agent 现状**：`--network none` 全断网
- **适用场景**：技能脚本需访问特定 API（如查天气、调 LLM）时，全断网太死
- **轻量落地**：`--add-host` + DNS 白名单，或 iptables 限定目标 IP
- **建议**：暂缓；Sidecar 对个人项目过重，DNS 白名单方案待真实需求出现再评估

### 5.3 不适用（明确说明为什么）

| 调研平台技术 | 不适用原因 |
|---|---|
| MicroVM (Kata/Firecracker) | data-agent 威胁模型是技能脚本，非恶意容器逃逸；MicroVM 部署运维对个人项目过重 |
| Nydus 懒加载 | data-agent 用 `python:3.11-slim` 小镜像，冷启动非瓶颈 |
| CRIU 进程快照 | Python 启动 ~200ms，fork 加速无意义 |
| BatchSandbox O(1) 批量 | data-agent 无批量创建场景 |
| 预热池 | 单机低并发，冷启动可接受；预热池常驻容器吃内存 |
| 三级配额 + 结算单元 | 个人项目无成本中心、无 to-B 多租户 |
| Leader 选主 / 灰度 | 单实例部署，无高可用需求 |
| E2B 协议兼容 | data-agent 非开放平台，无需让外部 SDK 接入 |

---

## 六、方法论借鉴

该平台团队总结了四条沙箱平台建设方法论，与 data-agent 的既定原则高度一致，可直接作为沙箱演进的
决策依据：

1. **工具先行，知识跟上**——核心工具成功率 80%+ 才上知识增强。data-agent sandbox 已是工具层，
   演进顺序正确
2. **能固定路径的不做自主规划**——确定性流程用内置流程锁定。data-agent `ScriptRunner` 协议 +
   固定 docker run 参数正是此思路
3. **知识晋升要有门槛**——使用频次高 ≠ 普适。data-agent 若做技能规格自适应，需多场景验证而非
   单次成功
4. **虚拟专家是蒸馏出来的**——经验从日常使用沉淀，非配置规则。对应 data-agent 技能匹配的
   embedding 召回持续优化方向

---

## 七、结论

该平台是 data-agent sandbox 的**生产级超大规模放大版**。核心隔离与生命周期思路（一次性容器 +
断网 + 只读 + 资源限额 + 超时兜底回收 + 协议抽象 + 故障降级）完全同构，被 17 万核生产环境验证。
data-agent 现有 `app/skills/sandbox.py` 的设计方向正确，量级选择合理。

**真正有价值的借鉴是三个轻量改进**，均低成本、对接现有 `TOOL_POLICIES` / `auth` / `Settings`，
不引入重依赖：

1. **幂等 token 防重试重复执行**（最高价值，对接工具熔断重试链路）
2. **资源规格预设档**（lite/standard/heavy，技能声明所需档位）
3. **工作空间并发配额**（防本机 OOM，对接 auth 工作空间）

MicroVM、预热池、Nydus、三级配额等大规模能力明确不适用——个人项目无需也不应引入这些复杂度。
