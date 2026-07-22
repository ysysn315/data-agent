"""FastAPI 依赖注入

提供用户认证、服务实例等依赖函数。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, status


async def get_current_user(
    authorization: str = Header(..., description="Bearer token")
) -> dict:
    """获取当前用户（必须登录）"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format"
        )

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is empty"
        )

    # TODO: 二期实现真实 token 验证
    return {"id": 1, "username": "dev_user", "token": token}


async def get_current_user_optional(
    authorization: str = Header(None, description="Bearer token")
) -> Optional[dict]:
    """获取当前用户（可选登录）"""
    if not authorization:
        return None

    if not authorization.startswith("Bearer "):
        return None

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None

    return {"id": 1, "username": "dev_user", "token": token}


async def get_skill_service():
    """获取 SkillService 实例（单例模式）"""
    global _skill_service_instance

    if _skill_service_instance is None:
        from app.skills.service import SkillService
        from app.skills.repository import InMemorySkillRepository
        from pathlib import Path

        repository = InMemorySkillRepository()
        service = SkillService(repository=repository)

        builtin_dir = Path(__file__).parent.parent / "skills" / "buildin"
        if builtin_dir.exists():
            await service.load_builtin_skills(builtin_dir)

        _skill_service_instance = service

    return _skill_service_instance


_skill_service_instance = None
