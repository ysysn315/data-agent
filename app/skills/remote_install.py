"""Skills 系统 - 远程安装（git clone 直下，不依赖 npx skills CLI）

相比 Yuxi 的实现（shell 出 npx CLI + 屏幕抓取输出）：
- git clone --depth 1 直接下载，无外部 CLI 依赖、输出解析稳定
- v2：整目录导入（含 scripts/ 等随附文件），不再只读 SKILL.md

安全约定：远程 skill 可能携带可执行脚本，而本项目脚本执行是无隔离的
subprocess（REQUIREMENTS §9）。因此**远程安装的 skill 默认 enabled=False**，
需要人工审查内容后通过 API 启用。
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Optional

from loguru import logger

from app.skills.models import Skill, SkillContent, SkillSourceType
from app.skills.service import SkillService

# GitHub URL 正则：https://github.com/owner/repo 或 owner/repo
GITHUB_PATTERN = re.compile(r"^(?:https?://github\.com/)?([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+?)(?:\.git)?$")

# 仓库内 skill 目录的查找路径（按优先级）
SKILL_SEARCH_ROOTS = ("skills", "", ".agents/skills")


class RemoteInstallError(Exception):
    """远程安装错误"""

    pass


def _normalize_source(source: str) -> tuple[str, str]:
    """解析远程仓库地址，返回 (owner, repo)"""
    source = source.strip()
    if not source:
        raise RemoteInstallError("source 不能为空")

    match = GITHUB_PATTERN.match(source)
    if not match:
        raise RemoteInstallError(
            f"source 格式错误，应为 'owner/repo' 或 'https://github.com/owner/repo'，当前: {source}"
        )

    return match.groups()


def _normalize_skill_name(skill: str) -> str:
    """验证 skill 名称（slug 格式）"""
    skill = skill.strip()
    if not skill:
        raise RemoteInstallError("skill 名称不能为空")

    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", skill):
        raise RemoteInstallError(
            f"skill 名称格式错误，应为小写字母、数字、连字符组合，如 'schema-retrieval'，当前: {skill}"
        )

    return skill


async def _run_git_clone(owner: str, repo: str, target_dir: Path, timeout: int = 60) -> None:
    """执行 git clone --depth 1"""
    url = f"https://github.com/{owner}/{repo}.git"
    logger.info(f"Cloning {url} to {target_dir}")

    process = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--depth",
        "1",
        url,
        str(target_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise RemoteInstallError(f"git clone 超时（{timeout}秒）")

    if process.returncode != 0:
        error_msg = stderr.decode("utf-8", errors="replace").strip()
        raise RemoteInstallError(f"git clone 失败: {error_msg}")

    logger.info(f"Clone 成功: {url}")


def _find_skill_dir(repo_dir: Path, skill_name: str) -> Optional[Path]:
    """在克隆的仓库中查找 skill 目录（skills/ → 根目录 → .agents/skills/）"""
    for root in SKILL_SEARCH_ROOTS:
        candidate = repo_dir / root / skill_name if root else repo_dir / skill_name
        if (candidate / "SKILL.md").exists():
            logger.info(f"找到 skill: {candidate}")
            return candidate

    logger.warning(f"未找到 skill '{skill_name}' 在仓库 {repo_dir}")
    return None


def _scan_skills(repo_dir: Path) -> list[dict[str, str]]:
    """扫描仓库中所有 SKILL.md 目录"""
    skills = []
    seen: set[str] = set()
    for root in SKILL_SEARCH_ROOTS:
        base = repo_dir / root if root else repo_dir
        if not base.is_dir():
            continue
        for skill_dir in sorted(base.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            try:
                parsed = SkillContent.parse(skill_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"解析 {skill_file} 失败: {e}")
                continue
            slug = parsed.frontmatter.slug
            if slug not in seen:
                seen.add(slug)
                skills.append(
                    {
                        "name": slug,
                        "description": parsed.frontmatter.description,
                    }
                )
    return skills


async def list_remote_skills(source: str, timeout: int = 60) -> list[dict[str, str]]:
    """列出远程仓库中所有可安装的 skills"""
    owner, repo = _normalize_source(source)

    with tempfile.TemporaryDirectory(prefix="remote-skills-") as temp_dir:
        repo_dir = Path(temp_dir) / "repo"
        await _run_git_clone(owner, repo, repo_dir, timeout)

        skills = _scan_skills(repo_dir)
        if not skills:
            raise RemoteInstallError(f"仓库 {source} 中未找到任何 SKILL.md")

        return skills


async def _import_one(
    skill_service: SkillService,
    repo_dir: Path,
    skill_name: str,
    source: str,
    user_id: Optional[int],
) -> Skill:
    """从已克隆仓库导入单个 skill（整目录，默认禁用）"""
    skill_dir = _find_skill_dir(repo_dir, skill_name)
    if not skill_dir:
        raise RemoteInstallError(f"仓库 {source} 中未找到 skill '{skill_name}'")

    try:
        skill = await skill_service.import_skill_dir(
            source_dir=skill_dir,
            source_type=SkillSourceType.REMOTE,
            user_id=user_id,
            enabled=False,  # 远程 skill 可能带可执行脚本，默认禁用待人工审查
        )
    except ValueError as e:
        raise RemoteInstallError(str(e))

    skill.share_config = {"source": source}
    if skill_service.repository:
        skill = await skill_service.repository.update(skill)

    logger.info(f"安装 skill 成功（默认禁用，需人工启用）: {skill.slug} (from {source})")
    return skill


async def install_remote_skill(
    source: str, skill_name: str, skill_service: SkillService, user_id: Optional[int] = None, timeout: int = 60
) -> Skill:
    """从远程仓库安装单个 skill（整目录导入，默认禁用）"""
    owner, repo = _normalize_source(source)
    skill_name = _normalize_skill_name(skill_name)

    existing = await skill_service.get_skill(skill_name)
    if existing:
        raise RemoteInstallError(f"Skill '{skill_name}' 已存在")

    with tempfile.TemporaryDirectory(prefix="remote-skills-") as temp_dir:
        repo_dir = Path(temp_dir) / "repo"
        await _run_git_clone(owner, repo, repo_dir, timeout)
        return await _import_one(skill_service, repo_dir, skill_name, source, user_id)


async def install_remote_skills_batch(
    source: str, skill_names: list[str], skill_service: SkillService, user_id: Optional[int] = None, timeout: int = 60
) -> list[dict]:
    """批量从同一个远程仓库安装多个 skills（只 clone 一次）

    返回与请求同序的结果列表：[{"slug": ..., "success": ..., "error": ...}]
    """
    owner, repo = _normalize_source(source)

    # 预校验所有 skill 名称
    results = []
    has_valid = False
    for name in skill_names:
        try:
            normalized = _normalize_skill_name(name)
            has_valid = True
            results.append({"slug": normalized, "success": False, "error": "unset"})
        except RemoteInstallError as e:
            results.append({"slug": name, "success": False, "error": str(e)})

    if not has_valid:
        return results

    with tempfile.TemporaryDirectory(prefix="remote-skills-") as temp_dir:
        repo_dir = Path(temp_dir) / "repo"

        try:
            await _run_git_clone(owner, repo, repo_dir, timeout)
        except RemoteInstallError as e:
            for result in results:
                if result["error"] == "unset":
                    result["error"] = str(e)
            return results

        for result in results:
            if result["error"] != "unset":
                continue  # 预校验失败的跳过

            name = result["slug"]
            try:
                skill = await _import_one(skill_service, repo_dir, name, source, user_id)
                result.update({"slug": skill.slug, "success": True, "error": None})
            except Exception as e:
                result["error"] = str(e)
                logger.error(f"安装 skill 失败 {name}: {e}")

    return results
