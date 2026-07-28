"""用户体系 + API Key 管理 - API 路由（F 轮）

- POST /api/auth/users          （admin）新建用户并签发 API Key，**明文只此一次返回**
- GET  /api/auth/users          （admin）列出用户，不回哈希/明文
- POST /api/auth/users/{id}/disable （admin）禁用用户（其 Key 随即失效）
- GET  /api/auth/me             校验自己的 Key，回显当前身份

demo（auth_enabled=False）下守卫恒放行、current_user 为占位 dev_user，
可用于本地联调；真正的鉴权在 auth_enabled=True 时生效。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core import auth
from app.core.dependencies import get_admin_user, get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


# ========== 请求/响应模型 ==========


class UserCreateRequest(BaseModel):
    """新建用户请求"""

    username: str = Field(..., description="用户名（唯一）")
    role: str = Field("member", description="角色：admin | member")
    workspace: str = Field("default", description="工作空间 slug（不存在则自动创建）")


class UserResponse(BaseModel):
    """用户信息（不含哈希/明文）"""

    id: int
    username: str
    role: str
    workspace_id: Optional[int] = None
    api_key_prefix: str = ""
    enabled: bool = True
    created_at: Optional[str] = None


class UserCreateResponse(UserResponse):
    """新建用户响应：额外携带**仅此一次**的明文 API Key"""

    api_key: str = Field(..., description="明文 API Key，只在创建时返回一次，请立即保存")


class MeResponse(BaseModel):
    """当前身份回显（demo 下为占位 dev_user，故大部分字段可空）"""

    id: int
    username: str
    role: str
    workspace_id: Optional[int] = None
    api_key_prefix: Optional[str] = None
    enabled: Optional[bool] = None


# ========== 用户管理（admin） ==========


@router.post(
    "/users",
    response_model=UserCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_admin_user)],
)
async def create_user(req: UserCreateRequest):
    """新建用户并签发 API Key（明文只在本响应出现一次）"""
    try:
        created = await auth.create_user(
            username=req.username,
            role=req.role,
            workspace=req.workspace,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return UserCreateResponse(**created)


@router.get("/users", response_model=list[UserResponse], dependencies=[Depends(get_admin_user)])
async def list_users():
    """列出全部用户（不回哈希/明文）"""
    return [UserResponse(**u) for u in await auth.list_users()]


@router.post(
    "/users/{user_id}/disable",
    response_model=UserResponse,
    dependencies=[Depends(get_admin_user)],
)
async def disable_user(user_id: int):
    """禁用用户（其 API Key 随即失效）"""
    updated = await auth.disable_user(user_id)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"用户不存在: {user_id}")
    return UserResponse(**updated)


# ========== 自身身份 ==========


@router.get("/me", response_model=MeResponse)
async def me(current_user: dict = Depends(get_current_user)):
    """校验自己的 Key 并回显当前身份（登录即可，无需 admin）"""
    return MeResponse(
        id=current_user.get("id"),
        username=current_user.get("username", ""),
        role=current_user.get("role", ""),
        workspace_id=current_user.get("workspace_id"),
        api_key_prefix=current_user.get("api_key_prefix"),
        enabled=current_user.get("enabled"),
    )
