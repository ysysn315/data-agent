"""Skills 系统 - 内存版数据访问层（Repository）

跳过数据库，用内存 dict 实现，用于开发和测试。
二期再切 PostgreSQL/SQLite。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.skills.models import Skill, SkillSourceType


class InMemorySkillRepository:
    """内存版 Skill 数据访问层"""

    def __init__(self):
        self._skills: dict[str, Skill] = {}  # slug -> Skill
        self._next_id: int = 1

    async def get_by_id(self, skill_id: int) -> Optional[Skill]:
        """按 ID 查询"""
        for skill in self._skills.values():
            if skill.id == skill_id:
                return skill
        return None

    async def get_by_slug(self, slug: str) -> Optional[Skill]:
        """按 slug 查询"""
        return self._skills.get(slug)

    async def list_all(
        self,
        enabled_only: bool = False,
        source_type: Optional[SkillSourceType] = None,
        limit: int = 100,
        offset: int = 0
    ) -> list[Skill]:
        """列表查询"""
        skills = list(self._skills.values())

        # 过滤启用状态
        if enabled_only:
            skills = [s for s in skills if s.enabled]

        # 过滤来源类型
        if source_type:
            skills = [s for s in skills if s.source_type == source_type]

        # 分页
        return skills[offset:offset + limit]

    async def list_enabled(self) -> list[Skill]:
        """查询所有启用的 skills"""
        return await self.list_all(enabled_only=True)

    async def list_accessible_by_user(
        self,
        user_id: int,
        include_global: bool = True
    ) -> list[Skill]:
        """
        查询用户可访问的 skills

        规则：
        - 系统内置（user_id=None）全局可访问
        - 用户自己创建的可访问
        - 其他用户创建的不可访问（除非 share_config 允许）
        """
        skills = []
        for skill in self._skills.values():
            # 系统内置
            if skill.user_id is None:
                if include_global:
                    skills.append(skill)
            # 用户自己创建
            elif skill.user_id == user_id:
                skills.append(skill)
            # 其他用户创建，检查 share_config
            else:
                # TODO: 实现 share_config 权限检查
                # 当前简化：不共享
                pass

        # 只返回启用的
        return [s for s in skills if s.enabled]

    async def create(self, skill: Skill) -> Skill:
        """创建 skill"""
        if skill.slug in self._skills:
            raise ValueError(f"Skill slug 已存在: {skill.slug}")

        skill.id = self._next_id
        self._next_id += 1
        skill.created_at = datetime.utcnow()
        skill.updated_at = datetime.utcnow()

        self._skills[skill.slug] = skill
        return skill

    async def update(self, skill: Skill) -> Skill:
        """更新 skill"""
        if skill.slug not in self._skills:
            raise ValueError(f"Skill 不存在: {skill.slug}")

        skill.updated_at = datetime.utcnow()
        self._skills[skill.slug] = skill
        return skill

    async def delete(self, slug: str) -> bool:
        """删除 skill"""
        if slug in self._skills:
            del self._skills[slug]
            return True
        return False

    async def enable(self, slug: str) -> bool:
        """启用 skill"""
        skill = self._skills.get(slug)
        if skill:
            skill.enabled = True
            skill.updated_at = datetime.utcnow()
            return True
        return False

    async def disable(self, slug: str) -> bool:
        """禁用 skill"""
        skill = self._skills.get(slug)
        if skill:
            skill.enabled = False
            skill.updated_at = datetime.utcnow()
            return True
        return False

    async def exists(self, slug: str) -> bool:
        """检查 slug 是否存在"""
        return slug in self._skills

    async def clear(self) -> None:
        """清空所有 skills（测试用）"""
        self._skills.clear()
        self._next_id = 1

    async def count(self) -> int:
        """统计数量"""
        return len(self._skills)


# 为了兼容，提供别名
SkillRepository = InMemorySkillRepository