"""技能脚本沙箱测试：命令构造 / 模式分发 / subprocess 兼容（离线） + 真实 Docker 容器（skipif）

离线部分不依赖 docker：DockerRunner.build_command 是纯函数；
"docker 未安装 / 守护进程未启动"用不存在的二进制与假 docker 脚本模拟。
真实容器部分在导入期探测 `docker info`，本机不可用时整组跳过。
"""

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.settings import Settings
from app.skills.sandbox import (
    CONTAINER_NAME_PREFIX,
    MAX_OUTPUT_CHARS,
    DockerRunner,
    SandboxUnavailableError,
    ScriptTimeoutError,
    SubprocessRunner,
    resolve_runner,
)
from app.skills.tools import create_skill_tools

BUILTIN_SQLITE_SKILL = Path(__file__).parent.parent / "app" / "skills" / "buildin" / "sqlite-query"


def _docker_available() -> bool:
    """探测 docker CLI 存在且守护进程可达（skipif 的判定依据）"""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


DOCKER_AVAILABLE = _docker_available()
requires_docker = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="本机 docker 不可用（未安装或守护进程未启动）")


def _make_skill(tmp_path: Path, script_name: str, code: str) -> Path:
    """构造一个只有 scripts/<script> 的最小技能目录"""
    skill_dir = tmp_path / "sandbox-skill"
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "scripts" / script_name).write_text(code, encoding="utf-8")
    return skill_dir


# ---------- 离线：DockerRunner 命令构造 ----------


def test_docker_command_contains_every_isolation_flag():
    """逐 flag 断言隔离参数：漏掉任何一个都意味着某类攻击面重新打开"""
    runner = DockerRunner(image="python:3.11-slim", memory="256m", cpus=0.5)
    cmd = runner.build_command(
        Path("/data/skills/sqlite-query"),
        "scripts/query.py",
        ["--db", "demo.db"],
        "data-agent-skill-abc123",
    )

    def value_of(flag: str) -> str:
        return cmd[cmd.index(flag) + 1]

    assert cmd[:2] == ["docker", "run"]
    assert "--rm" in cmd  # 正常退出即删容器
    assert value_of("--name") == "data-agent-skill-abc123"  # 命名容器，超时可点名回收
    assert value_of("--network") == "none"  # 断网
    assert value_of("--memory") == "256m"
    assert value_of("--cpus") == "0.5"
    assert value_of("--pids-limit") == "64"  # 防 fork 炸弹
    assert "--read-only" in cmd  # 根文件系统只读
    assert value_of("-v") == "/data/skills/sqlite-query:/skill:ro"  # 技能目录只读挂载
    assert value_of("-w") == "/skill"
    assert value_of("--tmpfs") == "/tmp"  # 唯一可写处

    # 镜像名之后是容器内命令：所有隔离 flag 必须出现在镜像名之前（否则会被当成脚本参数）
    image_index = cmd.index("python:3.11-slim")
    assert cmd[image_index + 1 :] == ["python", "scripts/query.py", "--db", "demo.db"]
    for flag in ("--rm", "--network", "--memory", "--cpus", "--pids-limit", "--read-only", "-v", "-w", "--tmpfs"):
        assert cmd.index(flag) < image_index, f"{flag} 落到了镜像名之后"


def test_docker_command_uses_configured_image_and_limits():
    runner = DockerRunner(image="custom/py:3.12", memory="512m", cpus=1.5)
    cmd = runner.build_command(Path("/s"), "scripts/a.py", [], "n")
    assert "custom/py:3.12" in cmd
    assert cmd[cmd.index("--memory") + 1] == "512m"
    assert cmd[cmd.index("--cpus") + 1] == "1.5"


# ---------- 离线：模式分发 ----------


def test_resolve_runner_defaults_to_subprocess():
    assert isinstance(resolve_runner(Settings(_env_file=None)), SubprocessRunner)


def test_resolve_runner_docker_mode_carries_settings():
    runner = resolve_runner(
        Settings(
            _env_file=None,
            skill_sandbox_mode="docker",
            skill_sandbox_image="custom:img",
            skill_sandbox_memory="128m",
            skill_sandbox_cpus=0.25,
        )
    )
    assert isinstance(runner, DockerRunner)
    cmd = runner.build_command(Path("/s"), "scripts/a.py", [], "n")
    assert "custom:img" in cmd
    assert cmd[cmd.index("--memory") + 1] == "128m"
    assert cmd[cmd.index("--cpus") + 1] == "0.25"


def test_settings_rejects_unknown_sandbox_mode():
    """枚举校验在配置装载层完成，非法值应在启动时报错而不是运行到一半"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, skill_sandbox_mode="firecracker")


# ---------- 离线：SubprocessRunner 与旧实现行为一致 ----------


async def test_subprocess_runner_executes_script(tmp_path):
    skill_dir = _make_skill(tmp_path, "hello.py", "print('你好，沙箱')")
    result = await SubprocessRunner().run(skill_dir, "scripts/hello.py", [], timeout=10)
    assert result.exit_code == 0
    assert result.stdout.strip() == "你好，沙箱"
    assert result.truncated is False


async def test_subprocess_runner_truncates_output_like_legacy(tmp_path):
    """截断文案与容器化前逐字节一致：前 8000 字符 + "共 N 字符"（N 是截断前总长）"""
    skill_dir = _make_skill(tmp_path, "spam.py", 'print("x" * 9000)')
    result = await SubprocessRunner().run(skill_dir, "scripts/spam.py", [], timeout=10)
    assert result.truncated is True
    # print 附带换行 → 原始输出 9001 字符
    assert result.stdout == "x" * MAX_OUTPUT_CHARS + "\n...（输出截断，共 9001 字符）"


async def test_subprocess_runner_reports_nonzero_exit(tmp_path):
    skill_dir = _make_skill(tmp_path, "boom.py", 'import sys\nsys.stderr.write("坏了")\nsys.exit(3)')
    result = await SubprocessRunner().run(skill_dir, "scripts/boom.py", [], timeout=10)
    assert result.exit_code == 3
    assert "坏了" in result.stderr


async def test_subprocess_runner_timeout_kills_process(tmp_path):
    skill_dir = _make_skill(tmp_path, "sleep.py", "import time\ntime.sleep(30)")
    with pytest.raises(ScriptTimeoutError):
        await SubprocessRunner().run(skill_dir, "scripts/sleep.py", [], timeout=0.5)


# ---------- 离线：docker 缺失 / 守护进程未启动的友好错误 ----------


async def test_docker_binary_missing_raises_chinese_error(tmp_path):
    skill_dir = _make_skill(tmp_path, "a.py", "print(1)")
    runner = DockerRunner(docker_bin=str(tmp_path / "no-such-docker"))
    with pytest.raises(SandboxUnavailableError, match="未找到 docker 命令"):
        await runner.run(skill_dir, "scripts/a.py", [], timeout=5)


async def test_docker_daemon_down_raises_chinese_error(tmp_path):
    """用假 docker 二进制模拟守护进程未启动（真实 CLI 此时 stderr 报 Cannot connect + 退出码 125）"""
    fake_docker = tmp_path / "fake-docker"
    fake_docker.write_text(
        "#!/bin/sh\necho 'docker: Cannot connect to the Docker daemon at unix:///var/run/docker.sock.' >&2\nexit 125\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    skill_dir = _make_skill(tmp_path, "a.py", "print(1)")
    runner = DockerRunner(docker_bin=str(fake_docker))
    with pytest.raises(SandboxUnavailableError, match="Docker 沙箱不可用"):
        await runner.run(skill_dir, "scripts/a.py", [], timeout=5)


async def test_tool_returns_friendly_error_instead_of_crashing(skill_service, tmp_path):
    """docker 缺失时工具层不抛异常，把中文提示当普通结果还给模型"""
    _, run_skill_script = create_skill_tools(
        skill_service, runner=DockerRunner(docker_bin=str(tmp_path / "missing-docker"))
    )
    output = await run_skill_script.ainvoke(
        {
            "slug": "sqlite-query",
            "script": "query.py",
            "script_args": [],
        }
    )
    assert "Docker 沙箱不可用" in output
    assert "subprocess" in output  # 提示里给出退路：改回 SKILL_SANDBOX_MODE=subprocess


# ---------- 离线：工具层分发与第一道闸 ----------


class _TimeoutRunner:
    async def run(self, *args, **kwargs):
        raise ScriptTimeoutError("超时")


class _MustNotRun:
    async def run(self, *args, **kwargs):
        raise AssertionError("路径校验应先拦截，不该走到执行器")


async def test_tool_timeout_message_unchanged(skill_service):
    """超时文案与容器化前逐字节一致"""
    _, run_skill_script = create_skill_tools(skill_service, runner=_TimeoutRunner())
    output = await run_skill_script.ainvoke({"slug": "sqlite-query", "script": "query.py"})
    assert output == "脚本执行超时（30s）: query.py"


async def test_path_check_happens_before_runner(skill_service):
    """路径包含校验是第一道闸：非法路径连执行器都不该碰到"""
    _, run_skill_script = create_skill_tools(skill_service, runner=_MustNotRun())
    output = await run_skill_script.ainvoke(
        {
            "slug": "sqlite-query",
            "script": "../../../etc/passwd",
        }
    )
    assert "非法脚本路径" in output or "脚本不存在" in output


async def test_default_runner_is_subprocess_and_executes(skill_service, demo_db):
    """不注入 runner 时按默认配置走 SubprocessRunner，行为与旧实现一致（与 test_skill_tools 互为印证）"""
    _, run_skill_script = create_skill_tools(skill_service)
    output = await run_skill_script.ainvoke(
        {
            "slug": "sqlite-query",
            "script": "query.py",
            "script_args": ["--db", demo_db, "--sql", "SELECT COUNT(*) AS n FROM orders"],
        }
    )
    assert json.loads(output)["rows"][0][0] == 3


# ---------- 真实 Docker 容器（本机 docker 可用才跑） ----------


@pytest.fixture(scope="module")
def docker_image() -> str:
    """确保沙箱镜像就绪；拉不下来（离线）则跳过，不让网络问题伪装成沙箱缺陷"""
    image = "python:3.11-slim"
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        pull = subprocess.run(["docker", "pull", image], capture_output=True, timeout=600)
        if pull.returncode != 0:
            pytest.skip(f"无法拉取镜像 {image}（网络不可用？）")
    return image


@requires_docker
async def test_docker_runs_sqlite_query_skill(tmp_path, docker_image):
    """端到端：真容器里跑内置 sqlite-query 的 query.py，出合法 JSON"""
    skill_dir = tmp_path / "sqlite-query"
    shutil.copytree(BUILTIN_SQLITE_SKILL, skill_dir)
    # 数据放进技能目录 —— docker 模式下容器只能看到 /skill（只读）
    conn = sqlite3.connect(skill_dir / "demo.db")
    conn.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, price REAL);"
        "INSERT INTO orders VALUES (1, 100.0), (2, 50.5), (3, 30.0);"
    )
    conn.commit()
    conn.close()

    runner = DockerRunner(image=docker_image)
    result = await runner.run(
        skill_dir.resolve(),
        "scripts/query.py",
        ["--db", "demo.db", "--sql", "SELECT COUNT(*) AS n FROM orders"],
        timeout=60,
    )
    assert result.exit_code == 0, f"stderr: {result.stderr}"
    assert json.loads(result.stdout)["rows"][0][0] == 3


@requires_docker
async def test_docker_readonly_blocks_writes_but_tmp_works(tmp_path, docker_image):
    """--read-only + :ro 挡住技能目录与根文件系统的写入；--tmpfs /tmp 是唯一可写处"""
    code = (
        "import sys\n"
        "try:\n"
        "    open(sys.argv[1], 'w').write('x')\n"
        "    print('wrote')\n"
        "    sys.exit(0)\n"
        "except OSError as e:\n"
        "    print(f'blocked: {e}')\n"
        "    sys.exit(42)\n"
    )
    skill_dir = _make_skill(tmp_path, "write.py", code).resolve()
    runner = DockerRunner(image=docker_image)

    ro_mount = await runner.run(skill_dir, "scripts/write.py", ["/skill/evil.txt"], timeout=60)
    assert ro_mount.exit_code == 42, "技能目录只读挂载未生效"
    assert not (skill_dir / "evil.txt").exists()  # 宿主机侧也确认没写进来

    ro_rootfs = await runner.run(skill_dir, "scripts/write.py", ["/etc/evil.txt"], timeout=60)
    assert ro_rootfs.exit_code == 42, "--read-only 根文件系统未生效"

    tmpfs = await runner.run(skill_dir, "scripts/write.py", ["/tmp/ok.txt"], timeout=60)
    assert tmpfs.exit_code == 0, f"tmpfs /tmp 应可写: {tmpfs.stdout} {tmpfs.stderr}"


@requires_docker
async def test_docker_network_none_blocks_connections(tmp_path, docker_image):
    """--network none 下对外连接立即失败（Network is unreachable）"""
    code = (
        "import socket, sys\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    print('connected')\n"
        "    sys.exit(0)\n"
        "except OSError as e:\n"
        "    print(f'blocked: {e}')\n"
        "    sys.exit(42)\n"
    )
    skill_dir = _make_skill(tmp_path, "net.py", code).resolve()
    result = await DockerRunner(image=docker_image).run(skill_dir, "scripts/net.py", [], timeout=60)
    assert result.exit_code == 42, f"断网未生效: {result.stdout}"


@requires_docker
async def test_docker_timeout_leaves_no_orphan_container(tmp_path, docker_image):
    """超时后 docker rm -f 兜底：docker ps -a 里不允许残留本项目前缀的容器"""
    skill_dir = _make_skill(tmp_path, "sleep.py", "import time\ntime.sleep(120)").resolve()
    with pytest.raises(ScriptTimeoutError):
        await DockerRunner(image=docker_image).run(skill_dir, "scripts/sleep.py", [], timeout=10)
    ps = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert CONTAINER_NAME_PREFIX not in ps.stdout, f"发现孤儿容器:\n{ps.stdout}"
