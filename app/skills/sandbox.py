"""技能脚本执行沙箱：ScriptRunner 抽象 + subprocess / Docker 一次性容器两种实现

REQUIREMENTS §9 一期决策是"沙箱太重，先本地 subprocess"；本模块补的就是它说的二期容器化。
设计与取舍详见 IMPLEMENTATION-sandbox.md，要点：

- ScriptRunner 是协议（Protocol），tools.py 的 run_skill_script 只依赖协议，
  路径包含校验留在工具层（第一道闸，与执行器无关）；
- SubprocessRunner 迁入原 run_skill_script 的执行逻辑，行为逐字节保持（默认模式，零新依赖）；
- DockerRunner 每次执行拉起一个一次性容器（--rm），断网 + 只读挂载 + 资源限额，
  用 docker CLI 而非 docker SDK（无新 Python 依赖，且 CLI + create_subprocess_exec 天然 async）；
- docker 未安装 / 守护进程未启动 → 抛 SandboxUnavailableError（中文提示），
  工具层转成普通文本返回给模型，不炸 agent 主流程。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from loguru import logger

# 执行侧常量（自 tools.py 迁入；Yuxi 对应 SANDBOX_EXEC_TIMEOUT_SECONDS=180 /
# SANDBOX_MAX_OUTPUT_BYTES=262144，见 backend.py:203-204。技能脚本负载轻，取更紧的值）
SCRIPT_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 8000

# Docker 沙箱固定参数（威胁模型见 IMPLEMENTATION-sandbox.md ②）
PIDS_LIMIT = "64"  # 防 fork 炸弹
CONTAINER_NAME_PREFIX = "data-agent-skill-"  # 容器名前缀 + 随机 ID，超时兜底回收时精确点名
_CLEANUP_TIMEOUT_SECONDS = 10
# docker run 自身失败（守护进程未启动等）的识别：约定退出码 125 + stderr 特征串
_DAEMON_ERROR_MARKS = ("Cannot connect to the Docker daemon", "error during connect")


class SandboxUnavailableError(RuntimeError):
    """沙箱环境不可用（docker 未安装 / 守护进程未启动等），message 是面向模型的中文提示"""


class ScriptTimeoutError(RuntimeError):
    """脚本执行超时（进程 / 容器已被清理，不会留孤儿）"""


@dataclass(frozen=True)
class RunResult:
    """一次脚本执行的结果。

    stdout 已按 MAX_OUTPUT_CHARS 截断（truncated=True 时末尾带"共 N 字符"说明，
    与容器化前 run_skill_script 的截断文案逐字节一致）；stderr 保留原文，由调用方决定摘取多少。
    """

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool


class ScriptRunner(Protocol):
    """脚本执行器协议：给定技能目录与目录内相对脚本路径，执行并回收资源"""

    async def run(
        self,
        skill_dir: Path,
        script_rel: str,
        args: Sequence[str],
        timeout: float = SCRIPT_TIMEOUT_SECONDS,
    ) -> RunResult: ...


def resolve_runner(settings) -> ScriptRunner:
    """按 settings.skill_sandbox_mode 选择执行器。

    枚举合法性由 Settings 的 Literal 类型在配置装载时校验，
    这里只需二分：docker → DockerRunner（携带镜像与资源配置），其余 → SubprocessRunner。
    """
    if settings.skill_sandbox_mode == "docker":
        return DockerRunner(
            image=settings.skill_sandbox_image,
            memory=settings.skill_sandbox_memory,
            cpus=settings.skill_sandbox_cpus,
        )
    return SubprocessRunner()


class SubprocessRunner:
    """本地进程直跑（默认模式，与容器化前的 run_skill_script 行为逐字节一致）

    无进程隔离，防护仅剩工具层路径校验 + 超时 + 截断 ——
    这正是"远程安装的技能默认禁用"的原因（skills-system.md §5）。
    """

    async def run(
        self,
        skill_dir: Path,
        script_rel: str,
        args: Sequence[str],
        timeout: float = SCRIPT_TIMEOUT_SECONDS,
    ) -> RunResult:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(skill_dir / script_rel),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(skill_dir),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise ScriptTimeoutError(f"脚本执行超时（{timeout}s）: {script_rel}")
        return _make_result(process.returncode, stdout, stderr)


class DockerRunner:
    """每次执行一个一次性容器：docker run --rm + 断网 + 只读挂载 + 资源限额

    不做 Yuxi 那种常驻 provisioner / 每 thread 容器（对本项目过重），
    容器生命周期被压缩成一条命令：正常路径 --rm 自动回收，
    超时路径 docker rm -f 按随机容器名兜底回收，两条路都不留孤儿。
    """

    def __init__(
        self,
        image: str = "python:3.11-slim",
        memory: str = "256m",
        cpus: float = 0.5,
        docker_bin: str = "docker",
    ):
        self._image = image
        self._memory = memory
        self._cpus = cpus
        self._docker_bin = docker_bin

    def build_command(self, skill_dir: Path, script_rel: str, args: Sequence[str], container_name: str) -> list[str]:
        """构造 docker run 命令行（纯函数，离线可测）。各 flag 的威胁模型见 IMPLEMENTATION-sandbox.md ②"""
        return [
            self._docker_bin,
            "run",
            "--rm",  # 正常退出即删容器
            "--name",
            container_name,  # 随机命名，超时兜底回收时点名用
            "--network",
            "none",  # 断网：防数据外带 / 反弹 shell / 恶意下载
            "--memory",
            self._memory,  # 内存上限
            "--cpus",
            str(self._cpus),  # CPU 配额
            "--pids-limit",
            PIDS_LIMIT,  # 防 fork 炸弹
            "--read-only",  # 根文件系统只读：防落盘持久化
            "-v",
            f"{skill_dir}:/skill:ro",  # 技能目录只读挂载（对标 Yuxi provisioner app.py:508）
            "-w",
            "/skill",  # 工作目录与 subprocess 模式的 cwd 语义一致
            "--tmpfs",
            "/tmp",  # 唯一可写处，容器退出即蒸发
            self._image,
            "python",
            script_rel,
            *args,
        ]

    async def run(
        self,
        skill_dir: Path,
        script_rel: str,
        args: Sequence[str],
        timeout: float = SCRIPT_TIMEOUT_SECONDS,
    ) -> RunResult:
        container_name = CONTAINER_NAME_PREFIX + uuid.uuid4().hex[:12]
        command = self.build_command(skill_dir, script_rel, args, container_name)
        logger.debug(f"docker 沙箱执行: {' '.join(command)}")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise SandboxUnavailableError(
                "Docker 沙箱不可用：未找到 docker 命令。请安装 Docker，或将 SKILL_SANDBOX_MODE 设回 subprocess"
            )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            # 杀掉 docker CLI 只是断开客户端，容器还在守护进程里跑 ——
            # 必须 docker rm -f 兜底，否则超时脚本会变成孤儿容器
            process.kill()
            await process.communicate()
            await self._force_remove(container_name)
            raise ScriptTimeoutError(f"脚本执行超时（{timeout}s），容器 {container_name} 已回收")

        stderr_text = stderr.decode("utf-8", errors="replace")
        if process.returncode == 125 or any(mark in stderr_text for mark in _DAEMON_ERROR_MARKS):
            # 125 是 docker run 自身失败的约定退出码（守护进程未启动 / 镜像不存在 / 参数错误），
            # 不是容器内脚本的退出码 —— 归类为环境问题而非脚本失败
            raise SandboxUnavailableError(
                f"Docker 沙箱不可用（docker run 失败）：{stderr_text.strip()[:300]}\n"
                f"请确认 Docker 守护进程已启动、镜像已拉取（docker pull {self._image}），"
                "或将 SKILL_SANDBOX_MODE 设回 subprocess"
            )
        return _make_result(process.returncode, stdout, stderr)

    async def _force_remove(self, container_name: str) -> None:
        """docker rm -f 强删容器；容器已随 --rm 消失时报错无害，静默忽略"""
        try:
            process = await asyncio.create_subprocess_exec(
                self._docker_bin,
                "rm",
                "-f",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=_CLEANUP_TIMEOUT_SECONDS)
            logger.info(f"超时容器已回收: {container_name}")
        except (asyncio.TimeoutError, OSError):
            logger.warning(f"回收容器 {container_name} 失败，请人工执行 docker rm -f {container_name}")


def _make_result(exit_code: int | None, stdout_bytes: bytes, stderr_bytes: bytes) -> RunResult:
    """解码 + 按 MAX_OUTPUT_CHARS 截断 stdout（截断文案与容器化前逐字节一致）"""
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    total = len(stdout)
    truncated = total > MAX_OUTPUT_CHARS
    if truncated:
        stdout = stdout[:MAX_OUTPUT_CHARS] + f"\n...（输出截断，共 {total} 字符）"
    return RunResult(
        exit_code=exit_code if exit_code is not None else -1, stdout=stdout, stderr=stderr, truncated=truncated
    )
