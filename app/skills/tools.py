"""Skills 系统 - Agent 侧工具

read_skill：渐进式披露的读取入口。本项目没有沙箱文件系统，
用一个专用工具替代 Yuxi 的 read_file + 只读 backend —— 天然只能读 skills，
SkillsMiddleware 拦截其调用结果作为激活信号。

run_skill_script：执行 skill 目录随附的脚本（对标 Yuxi mysql-reporter 的
`uv run scripts/query.py` 模式）。沙箱按 REQUIREMENTS §9 用本地 subprocess 替代，
仅做路径包含校验 + 超时 + 输出截断，无进程隔离 —— 因此远程安装的 skill 默认禁用。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool
from loguru import logger

SCRIPT_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 8000


def create_skill_tools(skill_service) -> list:
    """创建与 SkillService 绑定的基础工具（始终可见，不参与门控）"""

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

        scripts_dir = (Path(skill.dir_path) / "scripts").resolve()
        script_path = (scripts_dir / script).resolve()
        # 路径包含校验：脚本必须落在该技能的 scripts/ 目录内
        try:
            script_path.relative_to(scripts_dir)
        except ValueError:
            return f"非法脚本路径: {script}"
        if not script_path.is_file():
            return f"脚本不存在: {script}（目录: {scripts_dir}）"

        logger.info(f"执行 skill 脚本: {slug}/scripts/{script} args={script_args}")
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(script_path), *[str(a) for a in script_args],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(skill.dir_path),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=SCRIPT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return f"脚本执行超时（{SCRIPT_TIMEOUT_SECONDS}s）: {script}"

        output = stdout.decode("utf-8", errors="replace")
        if process.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:2000]
            return f"脚本执行失败（exit={process.returncode}）:\n{err}"

        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n...（输出截断，共 {len(output)} 字符）"
        return output or "(脚本无输出)"

    return [read_skill, run_skill_script]
