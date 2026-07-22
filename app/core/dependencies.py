"""FastAPI 依赖注入

提供用户认证、服务实例等依赖函数。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, status


async def get_current_user(
    authorization: str = Header(..., description="Bearer token")
) -> dict:
    """
    获取当前用户（必须登录）

    从 Authorization header 解析 token，返回用户信息。
    当前为简化实现，二期接入真实认证（JWT/OAuth）。

    Raises:
        HTTPException: 未提供 token 或 token 无效
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format, expected 'Bearer <token>'"
        )

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is empty"
        )

    # TODO: 二期实现真实 token 验证（JWT 解码、查询用户）
    # 当前简化：token 即用户 ID（仅用于开发测试）
    return {
        "id": 1,
        "username": "dev_user",
        "token": token
    }


async def get_current_user_optional(
    authorization: Optional[str] = Header(None, description="Bearer token")
) -> Optional[dict]:
    """
    获取当前用户（可选登录）

    如果提供了有效 token 则返回用户信息，否则返回 None。
    用于既支持匿名访问又支持登录用户的接口。

    Returns:
        用户信息 dict 或 None
    """
    if not authorization:
        return None

    if not authorization.startswith("Bearer "):
        return None

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None

    # TODO: 二期实现真实 token 验证
    return {
        "id": 1,
        "username": "dev_user",
        "token": token
    }


async def get_skill_service():
    """
    获取 SkillService 实例

    TODO: 二期接入依赖注入容器，当前每次创建新实例
    """
    from app.skills.service import SkillService
    return SkillService()