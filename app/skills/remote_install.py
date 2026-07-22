"""Skills 系统 - 远程安装（简化版，不依赖 npx skills CLI）

基于 Yuxi 设计，但直接用 git clone 下载，避免外部 CLI 依赖。
"""
from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from loguru import logger

from app.skills.models import Skill, SkillContent, SkillSourceType
from app.skills.service import SkillService


# GitHub URL 正则：https://github.com/owner/repo 或 owner/repo
GITHUB_PATTERN = re.compile(
    r"^(?:https?://github\.com/)?([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+?)(?:\.git)?$"
)


class RemoteInstallError(Exception):
    """远程安装错误"""
    pass


def _normalize_source(source: str) -> tuple[str, str]:
    """
    解析远程仓库地址

    Args:
        source: GitHub URL（https://github.com/owner/repo）或简写（owner/repo）

    Returns:
        (owner, repo) 元组

    Raises:
        RemoteInstallError: 格式错误
    """
    source = source.strip()
    if not source:
        raise RemoteInstallError("source 不能为空")

    # 匹配 GitHub URL 或简写
    match = GITHUB_PATTERN.match(source)
    if not match:
        raise RemoteInstallError(
            f"source 格式错误，应为 'owner/repo' 或 'https://github.com/owner/repo'，当前: {source}"
        )

    owner, repo = match.groups()
    return owner, repo


def _normalize_skill_name(skill: str) -> str:
    """
    验证 skill 名称（slug 格式）

    Args:
        skill: skill 名称

    Returns:
        验证后的名称

    Raises:
        RemoteInstallError: 格式错误
    """
    skill = skill.strip()
    if not skill:
        raise RemoteInstallError("skill 名称不能为空")

    # slug 格式：小写字母、数字、连字符
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", skill):
        raise RemoteInstallError(
            f"skill 名称格式错误，应为小写字母、数字、连字符组合，如 'schema-retrieval'，当前: {skill}"
        )

    return skill


async def _run_git_clone(
    owner: str,
    repo: str,
    target_dir: Path,
    timeout: int = 60
) -> None:
    """
    执行 git clone

    Args:
        owner: GitHub 用户名/组织名
        repo: 仓库名
        target_dir: 目标目录
        timeout: 超时秒数

    Raises:
        RemoteInstallError: clone 失败
    """
    url = f"https://github.com/{owner}/{repo}.git"
    logger.info(f"Cloning {url} to {target_dir}")

    process = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", url, str(target_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise RemoteInstallError(f"git clone 超时（{timeout}秒）")

    if process.returncode != 0:
        error_msg = stderr.decode("utf-8", errors="replace").strip()
        raise RemoteInstallError(f"git clone 失败: {error_msg}")

    logger.info(f"Clone 成功: {url}")


async def _find_skill_dir(
    repo_dir: Path,
    skill_name: str
) -> Optional[Path]:
    """
    在克隆的仓库中查找 skill 目录

    查找路径（按优先级）：
    1. skills/<skill_name>/SKILL.md
    2. <skill_name>/SKILL.md
    3. .agents/skills/<skill_name>/SKILL.md（Yuxi 风格）

    Args:
        repo_dir: 仓库根目录
        skill_name: skill 名称

    Returns:
        skill 目录路径，未找到返回 None
    """
    candidates = [
        repo_dir / "skills" / skill_name / "SKILL.md",
        repo_dir / skill_name / "SKILL.md",
        repo_dir / ".agents" / "skills" / skill_name / "SKILL.md",
    ]

    for candidate in candidates:
        if candidate.exists():
            logger.info(f"找到 skill: {candidate.parent}")
            return candidate.parent

    logger.warning(f"未找到 skill '{skill_name}' 在仓库 {repo_dir}")
    return None


async def list_remote_skills(
    source: str,
    timeout: int = 60
) -> list[dict[str, str]]:
    """
    列出远程仓库中所有可安装的 skills

    Args:
        source: GitHub 仓库地址
        timeout: git clone 超时

    Returns:
        skills 列表：[{"name": "skill-name", "description": "..."}]
    """
    owner, repo = _normalize_source(source)

    with tempfile.TemporaryDirectory(prefix="remote-skills-") as temp_dir:
        repo_dir = Path(temp_dir) / "repo"

        # Clone 仓库
        await _run_git_clone(owner, repo, repo_dir, timeout)

        # 查找所有 SKILL.md
        skills = []

        # 查找路径：skills/*/SKILL.md
        skills_dir = repo_dir / "skills"
        if skills_dir.exists():
            for skill_dir in skills_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    try:
                        content = skill_file.read_text(encoding="utf-8")
                        parsed = SkillContent.parse(content)
                        skills.append({
                            "name": parsed.frontmatter.slug,
                            "description": parsed.frontmatter.description
                        })
                    except Exception as e:
                        logger.warning(f"解析 {skill_file} 失败: {e}")

        # 查找路径：.agents/skills/*/SKILL.md（Yuxi 风格）
        agents_skills_dir = repo_dir / ".agents" / "skills"
        if agents_skills_dir.exists():
            for skill_dir in agents_skills_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    try:
                        content = skill_file.read_text(encoding="utf-8")
                        parsed = SkillContent.parse(content)
                        skills.append({
                            "name": parsed.frontmatter.slug,
                            "description": parsed.frontmatter.description
                        })
                    except Exception as e:
                        logger.warning(f"解析 {skill_file} 失败: {e}")

        if not skills:
            raise RemoteInstallError(f"仓库 {source} 中未找到任何 SKILL.md")

        return skills


async def install_remote_skill(
    source: str,
    skill_name: str,
    skill_service: SkillService,
    user_id: Optional[int] = None,
    timeout: int = 60
) -> Skill:
    """
    从远程仓库安装单个 skill

    Args:
        source: GitHub 仓库地址
        skill_name: skill 名称
        skill_service: Skills 业务逻辑实例
        user_id: 安装者用户 ID
        timeout: git clone 超时

    Returns:
        安装的 Skill 对象

    Raises:
        RemoteInstallError: 安装失败
    """
    owner, repo = _normalize_source(source)
    skill_name = _normalize_skill_name(skill_name)

    # 检查是否已存在
    existing = await skill_service.get_skill(skill_name)
    if existing:
        raise RemoteInstallError(f"Skill '{skill_name}' 已存在")

    with tempfile.TemporaryDirectory(prefix="remote-skills-") as temp_dir:
        repo_dir = Path(temp_dir) / "repo"

        # Clone 仓库
        await _run_git_clone(owner, repo, repo_dir, timeout)

        # 查找 skill 目录
        skill_dir = await _find_skill_dir(repo_dir, skill_name)
        if not skill_dir:
            raise RemoteInstallError(
                f"仓库 {source} 中未找到 skill '{skill_name}'"
            )

        # 读取 SKILL.md
        skill_file = skill_dir / "SKILL.md"
        content = skill_file.read_text(encoding="utf-8")

        # 解析并创建 skill
        try:
            parsed = SkillContent.parse(content)
        except Exception as e:
            raise RemoteInstallError(f"解析 SKILL.md 失败: {e}")

        # 创建 skill（来源标记为 remote）
        skill = Skill(
            slug=parsed.frontmatter.slug,
            name=parsed.frontmatter.name,
            description=parsed.frontmatter.description,
            content=content,
            source_type=SkillSourceType.REMOTE,
            enabled=True,
            user_id=user_id,
            share_config={"source": source}
        )

        # 如果有 repository，保存到数据库
        if skill_service.repository:
            skill = await skill_service.repository.create(skill)
        else:
            # 否则加到内置缓存（临时）
            skill_service._builtin_skills_cache[skill.slug] = skill

        logger.info(f"安装 skill 成功: {skill.slug} (from {source})")
        return skill


async def install_remote_skills_batch(
    source: str,
    skill_names: list[str],
    skill_service: SkillService,
    user_id: Optional[int] = None,
    timeout: int = 60
) -> list[dict]:
    """
    批量从同一个远程仓库安装多个 skills（只 clone 一次）

    Args:
        source: GitHub 仓库地址
        skill_names: skill 名称列表
        skill_service: Skills 业务逻辑实例
        user_id: 安装者用户 ID
        timeout: git clone 超时

    Returns:
        每个 skill 的安装结果：[{"slug": "...", "success": True/False, "error": "..."}]
    """
    owner, repo = _normalize_source(source)

    # 预校验所有 skill 名称
    results = []
    valid_skills = []
    for name in skill_names:
        try:
            normalized = _normalize_skill_name(name)
            valid_skills.append(normalized)
            results.append({"slug": normalized, "success": False, "error": "unset"})
        except RemoteInstallError as e:
            results.append({"slug": name, "success": False, "error": str(e)})

    if not valid_skills:
        return results

    with tempfile.TemporaryDirectory(prefix="remote-skills-") as temp_dir:
        repo_dir = Path(temp_dir) / "repo"

        # 只 clone 一次
        try:
            await _run_git_clone(owner, repo, repo_dir, timeout)
        except RemoteInstallError as e:
            # clone 失败，所有 skill 都失败
            for i, name in enumerate(skill_names):
                if results[i]["error"] == "unset":
                    results[i]["error"] = str(e)
            return results

        # 逐个安装
        for i, name in enumerate(skill_names):
            if results[i]["error"] != "unset":
                continue  # 预校验失败的跳过

            try:
                # 查找 skill 目录
                skill_dir = await _find_skill_dir(repo_dir, name)
                if not skill_dir:
                    raise RemoteInstallError(f"未找到 skill '{name}'")

                # 读取 SKILL.md
                skill_file = skill_dir / "SKILL.md"
                content = skill_file.read_text(encoding="utf-8")
                parsed = SkillContent.parse(content)

                # 创建 skill
                skill = Skill(
                    slug=parsed.frontmatter.slug,
                    name=parsed.frontmatter.name,
                    description=parsed.frontmatter.description,
                    content=content,
                    source_type=SkillSourceType.REMOTE,
                    enabled=True,
                    user_id=user_id,
                    share_config={"source": source}
                )

                # 保存
                if skill_service.repository:
                    skill = await skill_service.repository.create(skill)
                else:
                    skill_service._builtin_skills_cache[skill.slug] = skill

                results[i] = {"slug": skill.slug, "success": True}
                logger.info(f"安装 skill 成功: {skill.slug}")

            except Exception as e:
                results[i] = {"slug": name, "success": False, "error": str(e)}
                logger.error(f"安装 skill 失败 {name}: {e}")

    return results