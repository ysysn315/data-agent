"""Skills 系统 - API 路由"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.dependencies import (
    get_admin_user,
    get_current_user,
    get_current_user_optional,
    get_skill_service,
)
from app.core.settings import settings
from app.skills.models import SkillSourceType
from app.skills.remote_install import (
    RemoteInstallError,
    install_remote_skill,
    install_remote_skills_batch,
    list_remote_skills,
)
from app.skills.service import SkillService


router = APIRouter(prefix="/skills", tags=["skills"])

# 鉴权守卫（auth_enabled=True 时生效；demo 下恒放行）：
# 增删改 / 远程安装 => get_current_user（登录）；启停 => get_admin_user（管理员）。
# 读口（list/get/remote list）不挂守卫，保持开放。清单集中在 app/core/auth.PROTECTED_ENDPOINTS。


async def _tag_workspace(
    slug: str,
    current_user: Optional[dict],
    skill_service: SkillService,
) -> None:
    """工作空间隔离（lite）：把技能标记到当前用户的 workspace（复用 share_config，Yuxi 同款）。

    demo 下 current_user 为 None 或 workspace_id 为 None => 不打标（行为与从前一致）；
    auth 下写入 share_config["workspace_id"]，供 list_skills 过滤。内置技能不落库、不打标。
    选择复用 share_config JSON 而非给 skills 表加列：零迁移、与 D 轮 skills 表结构解耦，
    且 share_config 本就是"可见范围"语义的载体（对齐 yuxi-reference share_config.py 的 access_level 思路）。
    """
    ws = current_user.get("workspace_id") if current_user else None
    if ws is None or not skill_service.repository:
        return
    skill = await skill_service.get_skill(slug)
    if not skill or skill.source_type == SkillSourceType.BUILTIN:
        return
    skill.share_config = {**(skill.share_config or {}), "workspace_id": ws}
    await skill_service.repository.update(skill)


# ========== 请求/响应模型 ==========

class SkillCreateRequest(BaseModel):
    """创建 Skill 请求"""
    content: str = Field(..., description="SKILL.md 文件内容")


class SkillUpdateRequest(BaseModel):
    """更新 Skill 请求"""
    content: str = Field(..., description="SKILL.md 文件内容")


class RemoteInstallRequest(BaseModel):
    """远程安装请求"""
    source: str = Field(..., description="GitHub 仓库地址，如 'owner/repo'")
    skill: str = Field(..., description="Skill 名称")


class RemoteBatchInstallRequest(BaseModel):
    """批量远程安装请求"""
    source: str = Field(..., description="GitHub 仓库地址")
    skills: list[str] = Field(..., description="Skill 名称列表")


class SkillResponse(BaseModel):
    """Skill 响应"""
    id: Optional[int]
    slug: str
    name: str
    description: str
    source_type: str
    enabled: bool
    user_id: Optional[int]
    created_at: Optional[str]
    updated_at: Optional[str]


class SkillDetailResponse(SkillResponse):
    """Skill 详情响应（含完整内容）"""
    frontmatter: dict
    body: str


class RemoteSkillInfo(BaseModel):
    """远程 Skill 信息"""
    name: str
    description: str


class BatchInstallResult(BaseModel):
    """批量安装结果"""
    slug: str
    success: bool
    error: Optional[str] = None


# ========== API 接口 ==========

@router.get("", response_model=list[SkillResponse])
async def list_skills(
    enabled_only: bool = Query(True, description="只返回启用的"),
    source_type: Optional[str] = Query(None, description="按来源过滤：builtin/upload/remote"),
    skill_service: SkillService = Depends(get_skill_service),
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """
    列出所有 Skills

    支持按启用状态、来源类型过滤。auth 模式下按工作空间隔离：admin 全见；
    非 admin 仅见「本 workspace + 内置」。demo（auth_enabled=False）行为与从前完全一致。
    """
    if not settings.auth_enabled:
        # demo：完全保持现状（user_id 维度：无 header=>None=>list_all；Bearer=>dev_user 可见集）
        user_id = current_user.get("id") if current_user else None
        skills = await skill_service.list_skills(
            enabled_only=enabled_only,
            user_id=user_id,
        )
    else:
        # auth：取全部启用技能，再按角色/工作空间收窄
        skills = await skill_service.list_skills(enabled_only=enabled_only, user_id=None)
        if current_user and current_user.get("role") != "admin":
            ws = current_user.get("workspace_id")
            skills = [
                s for s in skills
                if s.source_type == SkillSourceType.BUILTIN
                or (s.share_config or {}).get("workspace_id") == ws
            ]

    # 按 source_type 过滤
    if source_type:
        try:
            source_enum = SkillSourceType(source_type)
            skills = [s for s in skills if s.source_type == source_enum]
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"无效的 source_type: {source_type}"
            )

    return [
        SkillResponse(
            id=skill.id,
            slug=skill.slug,
            name=skill.name,
            description=skill.description,
            source_type=skill.source_type.value,
            enabled=skill.enabled,
            user_id=skill.user_id,
            created_at=skill.created_at.isoformat() if skill.created_at else None,
            updated_at=skill.updated_at.isoformat() if skill.updated_at else None,
        )
        for skill in skills
    ]


@router.get("/{slug}", response_model=SkillDetailResponse)
async def get_skill(
    slug: str,
    skill_service: SkillService = Depends(get_skill_service),
):
    """获取单个 Skill 详情"""
    skill = await skill_service.get_skill(slug)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill 不存在: {slug}"
        )

    return SkillDetailResponse(
        id=skill.id,
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        source_type=skill.source_type.value,
        enabled=skill.enabled,
        user_id=skill.user_id,
        created_at=skill.created_at.isoformat() if skill.created_at else None,
        updated_at=skill.updated_at.isoformat() if skill.updated_at else None,
        frontmatter=skill.parsed.frontmatter.model_dump(),
        body=skill.parsed.body,
    )


@router.post(
    "",
    response_model=SkillResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_user)],
)
async def create_skill(
    request: SkillCreateRequest,
    skill_service: SkillService = Depends(get_skill_service),
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """创建 Skill（上传 SKILL.md 内容）"""
    user_id = current_user.get("id") if current_user else None

    try:
        skill = await skill_service.create_skill(
            content=request.content,
            user_id=user_id
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    await _tag_workspace(skill.slug, current_user, skill_service)

    return SkillResponse(
        id=skill.id,
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        source_type=skill.source_type.value,
        enabled=skill.enabled,
        user_id=skill.user_id,
        created_at=skill.created_at.isoformat() if skill.created_at else None,
        updated_at=skill.updated_at.isoformat() if skill.updated_at else None,
    )


@router.put("/{slug}", response_model=SkillResponse, dependencies=[Depends(get_current_user)])
async def update_skill(
    slug: str,
    request: SkillUpdateRequest,
    skill_service: SkillService = Depends(get_skill_service),
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """更新 Skill"""
    user_id = current_user.get("id") if current_user else None

    try:
        skill = await skill_service.update_skill(
            slug=slug,
            content=request.content,
            user_id=user_id
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e)
        )

    return SkillResponse(
        id=skill.id,
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        source_type=skill.source_type.value,
        enabled=skill.enabled,
        user_id=skill.user_id,
        created_at=skill.created_at.isoformat() if skill.created_at else None,
        updated_at=skill.updated_at.isoformat() if skill.updated_at else None,
    )


@router.delete(
    "/{slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_current_user)],
)
async def delete_skill(
    slug: str,
    skill_service: SkillService = Depends(get_skill_service),
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """删除 Skill"""
    user_id = current_user.get("id") if current_user else None

    try:
        deleted = await skill_service.delete_skill(slug=slug, user_id=user_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e)
        )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill 不存在: {slug}"
        )


@router.post("/{slug}/enable", response_model=SkillResponse, dependencies=[Depends(get_admin_user)])
async def enable_skill(
    slug: str,
    skill_service: SkillService = Depends(get_skill_service),
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """启用 Skill（触及"哪些技能对模型可见"，需 admin）"""
    # TODO: 实现启用逻辑
    skill = await skill_service.get_skill(slug)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill 不存在: {slug}"
        )

    skill.enabled = True
    if skill_service.repository:
        skill = await skill_service.repository.update(skill)

    return SkillResponse(
        id=skill.id,
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        source_type=skill.source_type.value,
        enabled=skill.enabled,
        user_id=skill.user_id,
        created_at=skill.created_at.isoformat() if skill.created_at else None,
        updated_at=skill.updated_at.isoformat() if skill.updated_at else None,
    )


@router.post("/{slug}/disable", response_model=SkillResponse, dependencies=[Depends(get_admin_user)])
async def disable_skill(
    slug: str,
    skill_service: SkillService = Depends(get_skill_service),
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """禁用 Skill（触及"哪些技能对模型可见"，需 admin）"""
    # TODO: 实现禁用逻辑
    skill = await skill_service.get_skill(slug)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill 不存在: {slug}"
        )

    skill.enabled = False
    if skill_service.repository:
        skill = await skill_service.repository.update(skill)

    return SkillResponse(
        id=skill.id,
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        source_type=skill.source_type.value,
        enabled=skill.enabled,
        user_id=skill.user_id,
        created_at=skill.created_at.isoformat() if skill.created_at else None,
        updated_at=skill.updated_at.isoformat() if skill.updated_at else None,
    )


# ========== 远程安装 ==========

@router.get("/remote/list", response_model=list[RemoteSkillInfo])
async def list_remote(
    source: str = Query(..., description="GitHub 仓库地址"),
):
    """列出远程仓库可安装的 Skills"""
    try:
        skills = await list_remote_skills(source)
    except RemoteInstallError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    return [
        RemoteSkillInfo(name=skill["name"], description=skill["description"])
        for skill in skills
    ]


@router.post(
    "/remote/install",
    response_model=SkillResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_user)],
)
async def install_remote(
    request: RemoteInstallRequest,
    skill_service: SkillService = Depends(get_skill_service),
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """从远程仓库安装单个 Skill"""
    user_id = current_user.get("id") if current_user else None

    try:
        skill = await install_remote_skill(
            source=request.source,
            skill_name=request.skill,
            skill_service=skill_service,
            user_id=user_id
        )
    except RemoteInstallError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    await _tag_workspace(skill.slug, current_user, skill_service)

    return SkillResponse(
        id=skill.id,
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        source_type=skill.source_type.value,
        enabled=skill.enabled,
        user_id=skill.user_id,
        created_at=skill.created_at.isoformat() if skill.created_at else None,
        updated_at=skill.updated_at.isoformat() if skill.updated_at else None,
    )


@router.post(
    "/remote/install-batch",
    response_model=list[BatchInstallResult],
    dependencies=[Depends(get_current_user)],
)
async def install_remote_batch(
    request: RemoteBatchInstallRequest,
    skill_service: SkillService = Depends(get_skill_service),
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """批量从远程仓库安装 Skills（只 clone 一次）"""
    user_id = current_user.get("id") if current_user else None

    results = await install_remote_skills_batch(
        source=request.source,
        skill_names=request.skills,
        skill_service=skill_service,
        user_id=user_id
    )

    # 安装成功的逐个打工作空间标记（auth 模式生效；demo 下 current_user 为 None 时跳过）
    for result in results:
        if result.get("success"):
            await _tag_workspace(result["slug"], current_user, skill_service)

    return [
        BatchInstallResult(
            slug=result["slug"],
            success=result["success"],
            error=result.get("error")
        )
        for result in results
    ]