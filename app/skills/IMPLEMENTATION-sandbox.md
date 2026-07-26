# 技能脚本沙箱速读（ScriptRunner 抽象 + Docker 一次性容器）

> 背景规格见 [docs/openspec/skills-system.md](../../docs/openspec/skills-system.md) §5、
> [REQUIREMENTS.md](../../REQUIREMENTS.md) §9 ——"Yuxi 的沙箱太重，先用本地 subprocess 替代，
> 二期再考虑容器化"。本文档就是那个二期：给 `run_skill_script` 换一个可插拔的执行底座，
> 默认仍是 subprocess 直跑（行为逐字节兼容），人工切到 docker 模式后脚本进一次性容器执行。

核心文件：

- `sandbox.py` —— `ScriptRunner` 协议 / `SubprocessRunner` / `DockerRunner` / `resolve_runner`
- `tools.py` —— `run_skill_script` 只负责路径校验与面向模型的文案，执行**委托**给 runner
- `app/core/settings.py` —— `skill_sandbox_*` 四项配置（`.env.example` 有对应注释块）

## ① 功能与配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `SKILL_SANDBOX_MODE` | `subprocess` | `subprocess` 本地直跑 / `docker` 一次性容器；非法值在启动装载配置时被 `Literal` 校验拒绝 |
| `SKILL_SANDBOX_IMAGE` | `python:3.11-slim` | 容器镜像，需**提前** `docker pull`（首跑现拉会吃掉超时预算） |
| `SKILL_SANDBOX_MEMORY` | `256m` | 容器内存上限（`--memory` 语法） |
| `SKILL_SANDBOX_CPUS` | `0.5` | CPU 配额（`--cpus`） |

两种模式共用同一条工具链路：`run_skill_script` 做完路径包含校验后，把
`(skill_dir, "scripts/<script>", args, timeout=30s)` 交给 `resolve_runner(settings)` 选出的执行器，
拿回 `RunResult(exit_code, stdout, stderr, truncated)` 再拼装文案。因此超时（30s）、
输出截断（8000 字符 + "共 N 字符"后缀）、错误文案在两种模式下完全一致 ——
`SubprocessRunner` 是原实现的原样迁移，现有 `test_skill_tools` 用例不改一行仍然全过。

docker 模式的行为差异只有一条须知：容器里**只能看到技能目录本身**（只读挂载为 `/skill`，
工作目录也是 `/skill`），宿主机其它路径（如 `SQLITE_DB_PATH` 指向的库文件）不可达，
需要数据的脚本要把数据放进技能目录。docker 未安装 / 守护进程未启动时不会崩：
runner 抛 `SandboxUnavailableError`（中文提示，含"改回 subprocess"的退路），
工具层接住转成普通文本还给模型。

## ② 实现原理：每个 flag 挡的是哪类攻击

docker 模式一次执行等价于（`DockerRunner.build_command`，纯函数、离线可测）：

```
docker run --rm --name data-agent-skill-<hex12> --network none \
  --memory 256m --cpus 0.5 --pids-limit 64 --read-only \
  -v <skill_dir>:/skill:ro -w /skill --tmpfs /tmp \
  python:3.11-slim python scripts/<script> <args...>
```

| flag | 威胁模型：挡什么 |
|---|---|
| `--network none` | 数据外带（把查到的业务数据 POST 出去）、反弹 shell、下载二阶段载荷 —— 恶意技能脚本最有价值的三件事全靠网络 |
| `-v <skill_dir>:/skill:ro` | 篡改技能自身：改 `SKILL.md` 给下次激活投毒、改 `scripts/` 给下次执行埋雷（技能目录是宿主机持久数据，必须只读） |
| `--read-only` | 根文件系统落盘持久化（写 crontab / 植入文件等容器内驻留手段），也顺带挡住污染镜像层 |
| `--tmpfs /tmp` | 不是防护而是配套：只读根之下给合法脚本留唯一可写的暂存处，容器退出即蒸发，不回流宿主机 |
| `--memory` / `--cpus` | 资源耗尽：死循环 / 大内存分配拖垮宿主机上的 agent 服务本体 |
| `--pids-limit 64` | fork 炸弹（资源限额里唯一挡不住的进程数维度单独设限） |
| `--rm` + `--name 随机` | 生命周期：正常退出即删不留残骸；随机命名让超时兜底能精确点名（见下） |

**超时双保险与孤儿容器回收**：`asyncio.wait_for` 超时后 `process.kill()` 杀的只是 docker CLI ——
CLI 只是 attach 到守护进程的客户端，**容器本体还在跑**，这正是孤儿容器的来源。
所以超时路径必须补一刀 `docker rm -f <container_name>`（`_force_remove`，静默容忍
"容器已随 --rm 消失"的报错）。两条回收路径：正常退出走 `--rm`（守护进程侧自动删），
超时走 `rm -f`（杀 + 删一步到位）；测试 `test_docker_timeout_leaves_no_orphan_container`
在超时后断言 `docker ps -a` 无本项目前缀容器。

**环境不可用的判定**：`FileNotFoundError`（没装 docker）直接转 `SandboxUnavailableError`；
拉起成功但 `docker run` 自身失败的约定退出码是 125（守护进程未启动 / 镜像不存在 / 参数错误），
连同 stderr 的 "Cannot connect to the Docker daemon" 特征串一起识别 —— 125 是 docker 的保留码，
不会与正常脚本退出码混淆（脚本 `sys.exit(125)` 属自找歧义的边缘情况，报错里附 stderr 原文可辨）。

## ③ Yuxi 怎么做的，我们为何不同

参考仓库（只读）：`/Users/ysn/projects/yuxi-reference`。

- **常驻 provisioner 服务 + 每 thread 一个持久容器**：`docker/sandbox_provisioner/app.py`
  是独立 HTTP 服务，用 docker SDK（`self._client.containers.run`，:521）管理容器全生命周期 ——
  labels 标记归属（:495-503）、每沙箱专属 network（:510）、`/home/gem` 挂 tmpfs（:514）、
  发现/健康检查/重建（:460-479、:535 起）。技能目录只读挂载在 **app.py:508**：
  `{"bind": "/home/gem/skills", "mode": "ro"}` —— 我们的 `-v <skill_dir>:/skill:ro`
  就是这一行的一次性版。
- **执行侧超时/截断/路径白名单**：`backend/package/yuxi/agents/backends/sandbox/backend.py`
  的常量在 **:203-204**（`SANDBOX_EXEC_TIMEOUT_SECONDS=180`、`SANDBOX_MAX_OUTPUT_BYTES=262144`，
  `execute()` :375-403 截断输出并回带 `truncated` 标志 —— 我们 `RunResult.truncated` 同构）；
  虚拟路径白名单在 **:40-44**（`_READABLE_ROOTS` / `_WRITABLE_ROOTS`）加 **:50-59** 拒 `..`
  （与我们工具层的 `relative_to` 包含校验同职责，只是它作用于沙箱内虚拟路径）。
- **为什么不搬**：Yuxi 的沙箱服务的是"通用 Agent 长会话里反复执行代码、读写工作区文件"，
  容器复用把冷启动摊薄到整个会话，代价是一整套常驻设施 —— provisioner 服务本体、
  容器发现与健康检查、脏挂载重建、跨会话状态残留面。本项目的负载只有
  "技能脚本一次跑完出结果"这一种，没有会话内累积状态可复用，常驻容器纯付成本不得收益；
  一次性容器把全部生命周期管理压缩成"一条命令 + 一条兜底命令"。

## ④ 取舍

- **docker CLI 而非 docker SDK**：硬约束是本轮不加 Python 依赖（docker SDK 连带 requests 栈
  与 API 版本协商的耦合）；同时我们只用 `run` / `rm -f` 两个动词，CLI 就是完备接口，
  `asyncio.create_subprocess_exec` 调 CLI 天然异步（SDK 是同步阻塞，还得再包 `to_thread`）。
  代价：错误只能按退出码 + stderr 文本判定（见 ② 的 125 约定），没有类型化异常 —— 对
  两个动词的使用面，这个代价可控。
- **一次性 vs 常驻**：每次执行多付 ~1s 容器冷启动，换来零守护进程、零容器状态、
  天然并发隔离（两个脚本两个容器互不感知）、崩溃后无需清理存量。技能脚本本身以
  秒级 IO/查询为主，1s 摊在 30s 超时预算里可接受；真出现高频调用再考虑容器池。
- **默认 subprocess 的兼容性**：无 docker 的开发机 / CI 完全不受影响 —— 默认模式行为
  与容器化前逐字节一致（含截断与错误文案），存量测试零改动通过；docker 相关测试
  `skipif` 探测 `docker info`，命令构造类用例离线可测。沙箱是"可启用的加固"而非新的准入门槛。
- **与"远程技能默认禁用"的纵深防御关系**：`remote_install.py` 安装的技能默认
  `enabled=False`（人工审查后经 API 启用）—— 这是**准入闸**，沙箱不取代它，也不改它。
  沙箱是准入之后的**运行闸**：审查有疏漏、或被审查过的技能后续被改坏时，docker 模式下
  脚本仍然断网、只读、限额，翻不出容器。三道闸依次为：默认禁用（准入）→
  工具层路径包含校验（只能执行技能自己 `scripts/` 里的文件，与执行器无关）→
  容器隔离（执行时兜底）。subprocess 模式没有第三道闸，这正是它保持默认、
  而远程技能启用前需要人工审查的原因。
- **已知限制 / 后续硬化方向**：容器内以镜像默认用户（root）运行，靠只读挂载与默认
  capability 集约束，未做 `--user` 降权与 seccomp 定制；数据文件必须放进技能目录，
  未提供"额外数据目录只读挂载"配置；镜像需人工预拉取。以上均可在不动 `ScriptRunner`
  协议的前提下增量补齐。
