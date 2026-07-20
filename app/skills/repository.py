"""Skills 系统 - 数据访问层（Repository）"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.skills.models import Skill, SkillSourceType


class SkillRepository:
    """Skill 数据访问层"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, skill_id: int) -> Optional[Skill]:
        """按 ID 查询"""
        # TODO: 实现 SQLAlchemy 查询
        # 当前先返回 None，等数据库表创建后实现
        return None

    async def get_by_slug(self, slug: str) -> Optional[Skill]:
        """按 slug 查询"""
        # TODO: 实现 SQLAlchemy 查询
        return None

    async def list_all(
        self,
        enabled_only: bool = False,
        source_type: Optional[SkillSourceType] = None,
        limit: int = 100,
        offset: int = 0
    ) -> list[Skill]:
        """列表查询"""
        # TODO: 实现 SQLAlchemy 查询
        return []

    async def list_enabled(self) -> list[Skill]:
        """查询所有启用的 skills"""
        return await self.list_all(enabled_only=True)

    async def list_accessible_by_user(
        self,
        user_id: int,
        include_global: bool = True
    ) -> list[Skill]:
        """查询用户可访问的 skills（全局 + 用户创建）"""
        # TODO: 实现权限过滤
        return await self.list_enabled()

    async def create(self, skill: Skill) -> Skill:
        """创建 skill"""
        # TODO: 实现 SQLAlchemy 插入
        skill.created_at = datetime.utcnow()
        skill.updated_at = datetime.utcnow()
        return skill

    async def update(self, skill: Skill) -> Skill:
        """更新 skill"""
        # TODO: 实现 SQLAlchemy 更新
        skill.updated_at = datetime.utcnow()
        return skill

    async def delete(self, slug: str) -> bool:
        """删除 skill"""
        # TODO: 实现 SQLAlchemy 删除
        return True

    async def enable(self, slug: str) -> bool:
        """启用 skill"""
        # TODO: 实现
        return True

    async def disable(self, slug: str) -> bool:
        """禁用 skill"""
        # TODO: 实现
        return True

    async def exists(self, slug: str) -> bool:
        """检查 slug 是否存在"""
        skill = await self.get_by_slug(slug)
        return skill is not None