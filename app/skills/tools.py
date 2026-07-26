"""Skills 系统 - Agent 侧工具

read_skill：渐进式披露的读取入口。本项目没有沙箱文件系统，
用一个专用工具替代 Yuxi 的 read_file + 只读 backend —— 天然只能读 skills，
SkillsMiddleware 拦截其调用结果作为激活信号。

run_skill_script：执行 skill 目录随附的脚本（对标 Yuxi mysql-reporter 的
`uv run scripts/query.py` 模式）。执行底座由 app/skills/sandbox.py 的 ScriptRunner
按 SKILL_SANDBOX_MODE 分发：subprocess 直跑（默认，REQUIREMENTS §9 一期决策）
或 docker 一次性容器（§9 说的二期容器化：断网 + 只读挂载 + 资源限额）。
路径包含校验留在本层 —— 第一道闸，与执行器无关；
"远程安装的 skill 默认禁用"不变，沙箱是人工启用之后的第二道防线（纵深防御）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from langchain_core.tools import tool
from loguru import logger

from app.core.settings import get_settings
from app.skills.sandbox import (
    SCRIPT_TIMEOUT_SECONDS,
    SandboxUnavailableError,
    ScriptRunner,
    ScriptTimeoutError,
    resolve_runner,
)


def create_skill_tools(skill_service, runner: Optional[ScriptRunner] = None) -> list:
    """创建与 SkillService 绑定的基础工具（始终可见，不参与门控）

    runner: 脚本执行器；缺省按全局 settings 的 skill_sandbox_mode 解析
    （subprocess / docker），测试可注入假执行器。
    """
    script_runner = runner or resolve_runner(get_settings())

    @tool
    async def read_skill(slug: str) -> str:
        """读取指定技能（Skill）的完整说明文档。

        使用任何技能前必须先调用本工具读取其 SKILL.md 全文，
        这会激活该技能并解锁其声明的专用工具。

        参数:
            slug: 技能的唯一标识，如 "sqlite-query"
        """
        body = await skill_service.get_skill_body(slug)
        if body is None:
            return f"技能不存在或未启用: {slug}"
        return body

    @tool
    async def run_skill_script(slug: str, script: str, script_args: Optional[list[str]] = None) -> str:
        """执行技能目录下 scripts/ 里的脚本，返回其输出。

        仅当技能的 SKILL.md 中说明了脚本用法时使用。
        脚本在受限环境中运行（docker 沙箱模式下无网络、文件系统只读，
        只能读取技能目录本身）；超时或沙箱不可用时返回中文错误说明。

        参数:
            slug: 技能唯一标识
            script: 脚本文件名，如 "query.py"
            script_args: 传给脚本的命令行参数列表
        """
        script_args = script_args or []
        skill = await skill_service.get_skill(slug)
        if not skill or not skill.enabled:
            return f"技能不存在或未启用: {slug}"
        if not skill.dir_path:
            return f"技能 {slug} 没有目录，无脚本可执行"

        skill_dir = Path(skill.dir_path).resolve()
        scripts_dir = (skill_dir / "scripts").resolve()
        script_path = (scripts_dir / script).resolve()
        # 路径包含校验：脚本必须落在该技能的 scripts/ 目录内（第一道闸，与执行器无关）
        try:
            script_path.relative_to(scripts_dir)
        except ValueError:
            return f"非法脚本路径: {script}"
        if not script_path.is_file():
            return f"脚本不存在: {script}（目录: {scripts_dir}）"

        # 校验通过后才折算目录内相对路径，交给执行器（docker 模式在容器内以 /skill 为根）
        script_rel = (Path("scripts") / script_path.relative_to(scripts_dir)).as_posix()
        logger.info(
            f"执行 skill 脚本: {slug}/{script_rel} args={script_args} "
            f"runner={type(script_runner).__name__}"
        )
        try:
            result = await script_runner.run(
                skill_dir, script_rel, [str(a) for a in script_args],
                timeout=SCRIPT_TIMEOUT_SECONDS,
            )
        except ScriptTimeoutError:
            return f"脚本执行超时（{SCRIPT_TIMEOUT_SECONDS}s）: {script}"
        except SandboxUnavailableError as e:
            return str(e)

        if result.exit_code != 0:
            return f"脚本执行失败（exit={result.exit_code}）:\n{result.stderr[:2000]}"
        return result.stdout or "(脚本无输出)"

    return [read_skill, run_skill_script]
