"""Skills 系统 - 业务逻辑层（Service）"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from loguru import logger

from app.skills.models import (
    ExpandedSkills,
    Skill,
    SkillContent,
    SkillDependencyNode,
    SkillSourceType,
)
from app.skills.repository import SkillRepository


class SkillService:
    """Skills 业务逻辑：加载、解析、依赖展开"""

    def __init__(self, repository: Optional[SkillRepository] = None):
        self.repository = repository
        self._builtin_skills_cache: dict[str, Skill] = {}
        self._cache_lock = asyncio.Lock()

    async def load_builtin_skills(self, builtin_dir: str | Path) -> list[Skill]:
        """
        加载内置 Skills（从文件系统）

        Args:
            builtin_dir: 内置 skills 目录路径，如 "app/skills/buildin"
        """
        builtin_path = Path(builtin_dir)
        if not builtin_path.exists():
            logger.warning(f"内置 skills 目录不存在: {builtin_dir}")
            return []

        skills = []
        for skill_dir in builtin_path.iterdir():
            if not skill_dir.is_dir():
                continue

            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                logger.debug(f"跳过无 SKILL.md 的目录: {skill_dir.name}")
                continue

            try:
                skill = await self._load_skill_from_file(skill_file)
                skill.source_type = SkillSourceType.BUILTIN
                skill.user_id = None  # 系统内置
                skills.append(skill)
                self._builtin_skills_cache[skill.slug] = skill
                logger.info(f"加载内置 skill: {skill.slug}")
            except Exception as e:
                logger.error(f"加载 skill 失败 {skill_dir.name}: {e}")

        return skills

    async def _load_skill_from_file(self, file_path: Path) -> Skill:
        """从文件加载 skill"""
        content = file_path.read_text(encoding="utf-8")
        parsed = SkillContent.parse(content)

        return Skill(
            slug=parsed.frontmatter.slug,
            name=parsed.frontmatter.name,
            description=parsed.frontmatter.description,
            content=content,
            source_type=SkillSourceType.BUILTIN,
            enabled=True
        )

    async def get_skill(self, slug: str) -> Optional[Skill]:
        """
        获取 skill（优先缓存，其次数据库，最后内置）
        """
        # 1. 查内置缓存
        if slug in self._builtin_skills_cache:
            return self._builtin_skills_cache[slug]

        # 2. 查数据库（如果有 repository）
        if self.repository:
            skill = await self.repository.get_by_slug(slug)
            if skill:
                return skill

        return None

    async def list_skills(
        self,
        enabled_only: bool = True,
        user_id: Optional[int] = None
    ) -> list[Skill]:
        """
        列表查询 skills

        Args:
            enabled_only: 只返回启用的
            user_id: 用户 ID，用于权限过滤（None=返回所有）
        """
        skills = list(self._builtin_skills_cache.values())

        if self.repository:
            if user_id:
                db_skills = await self.repository.list_accessible_by_user(user_id)
            else:
                db_skills = await self.repository.list_all(enabled_only=enabled_only)
            skills.extend(db_skills)

        # 去重（slug 唯一）
        seen = set()
        unique_skills = []
        for skill in skills:
            if skill.slug not in seen:
                seen.add(skill.slug)
                if not enabled_only or skill.enabled:
                    unique_skills.append(skill)

        return unique_skills

    async def expand_dependencies(
        self,
        skill_slugs: list[str],
        max_depth: int = 3
    ) -> ExpandedSkills:
        """
        展开 skills 依赖（递归）

        Args:
            skill_slugs: 初始 skill slug 列表
            max_depth: 最大递归深度（防止循环依赖）

        Returns:
            ExpandedSkills: 展开后的 skills、tools、mcps、prompt
        """
        expanded = ExpandedSkills()
        visited: set[str] = set()
        queue: list[SkillDependencyNode] = [
            SkillDependencyNode(slug=slug, depth=0)
            for slug in skill_slugs
        ]

        while queue:
            node = queue.pop(0)

            # 防止循环依赖
            if node.slug in visited:
                logger.warning(f"检测到循环依赖，跳过: {node.slug}")
                continue
            if node.depth > max_depth:
                logger.warning(f"超过最大依赖深度 {max_depth}，跳过: {node.slug}")
                continue

            visited.add(node.slug)

            # 加载 skill
            skill = await self.get_skill(node.slug)
            if not skill:
                logger.warning(f"Skill 不存在: {node.slug}")
                continue
            if not skill.enabled:
                logger.info(f"Skill 已禁用: {node.slug}")
                continue

            # 添加到展开结果
            expanded.add_skill(skill)

            # 递归展开依赖的 skills
            for dep_slug in skill.get_skills():
                queue.append(SkillDependencyNode(
                    slug=dep_slug,
                    depth=node.depth + 1
                ))

        expanded.deduplicate()
        return expanded

    async def match_skills_by_query(
        self,
        query: str,
        candidate_slugs: Optional[list[str]] = None,
        top_k: int = 3
    ) -> list[Skill]:
        """
        根据用户查询匹配 skills（语义匹配）

        简单实现：按 slug/name/description 关键词匹配
        二期可用 embedding 语义匹配

        Args:
            query: 用户查询文本
            candidate_slugs: 候选 skill slug 列表（None=所有启用的）
            top_k: 返回 top-k 匹配结果
        """
        if candidate_slugs:
            skills = []
            for slug in candidate_slugs:
                skill = await self.get_skill(slug)
                if skill and skill.enabled:
                    skills.append(skill)
        else:
            skills = await self.list_skills(enabled_only=True)

        # 简单关键词匹配（二期改 embedding）
        query_lower = query.lower()
        scored_skills = []

        for skill in skills:
            score = 0
            # slug 匹配
            if skill.slug in query_lower:
                score += 10
            # name 匹配
            if skill.name.lower() in query_lower:
                score += 5
            # description 关键词匹配
            desc_words = skill.description.lower().split()
            query_words = query_lower.split()
            common_words = set(desc_words) & set(query_words)
            score += len(common_words)

            if score > 0:
                scored_skills.append((skill, score))

        # 按分数排序
        scored_skills.sort(key=lambda x: x[1], reverse=True)
        return [skill for skill, _ in scored_skills[:top_k]]

    async def create_skill(
        self,
        content: str,
        user_id: Optional[int] = None
    ) -> Skill:
        """
        创建 skill（从 SKILL.md 内容）

        Args:
            content: SKILL.md 文件内容
            user_id: 创建者用户 ID
        """
        parsed = SkillContent.parse(content)

        # 检查 slug 是否已存在
        existing = await self.get_skill(parsed.frontmatter.slug)
        if existing:
            raise ValueError(f"Skill slug 已存在: {parsed.frontmatter.slug}")

        skill = Skill(
            slug=parsed.frontmatter.slug,
            name=parsed.frontmatter.name,
            description=parsed.frontmatter.description,
            content=content,
            source_type=SkillSourceType.UPLOAD,
            enabled=True,
            user_id=user_id
        )

        if self.repository:
            skill = await self.repository.create(skill)

        return skill

    async def update_skill(
        self,
        slug: str,
        content: str,
        user_id: Optional[int] = None
    ) -> Skill:
        """更新 skill"""
        existing = await self.get_skill(slug)
        if not existing:
            raise ValueError(f"Skill 不存在: {slug}")

        # 权限检查：只有创建者或管理员能更新
        if existing.user_id and existing.user_id != user_id:
            raise PermissionError(f"无权限更新 skill: {slug}")

        parsed = SkillContent.parse(content)
        existing.name = parsed.frontmatter.name
        existing.description = parsed.frontmatter.description
        existing.content = content

        if self.repository:
            existing = await self.repository.update(existing)

        return existing

    async def delete_skill(self, slug: str, user_id: Optional[int] = None) -> bool:
        """删除 skill"""
        existing = await self.get_skill(slug)
        if not existing:
            return False

        # 内置 skill 不能删除
        if existing.source_type == SkillSourceType.BUILTIN:
            raise ValueError("内置 skill 不能删除")

        # 权限检查
        if existing.user_id and existing.user_id != user_id:
            raise PermissionError(f"无权限删除 skill: {slug}")

        if self.repository:
            return await self.repository.delete(slug)

        return True