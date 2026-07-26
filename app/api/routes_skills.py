"""Skills 系统 - API 路由"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.dependencies import get_current_user_optional, get_skill_service
from app.skills.models import SkillSourceType
from app.skills.remote_install import (
    RemoteInstallError,
    install_remote_skill,
    install_remote_skills_batch,
    list_remote_skills,
)
from app.skills.service import SkillService


router = APIRouter(prefix="/skills", tags=["skills"])


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

    支持按启用状态、来源类型过滤
    """
    user_id = current_user.get("id") if current_user else None
    skills = await skill_service.list_skills(
        enabled_only=enabled_only,
        user_id=user_id
    )

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


@router.post("", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
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


@router.put("/{slug}", response_model=SkillResponse)
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


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
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


@router.post("/{slug}/enable", response_model=SkillResponse)
async def enable_skill(
    slug: str,
    skill_service: SkillService = Depends(get_skill_service),
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """启用 Skill"""
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


@router.post("/{slug}/disable", response_model=SkillResponse)
async def disable_skill(
    slug: str,
    skill_service: SkillService = Depends(get_skill_service),
    current_user: Optional[dict] = Depends(get_current_user_optional),
):
    """禁用 Skill"""
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


@router.post("/remote/install", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
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


@router.post("/remote/install-batch", response_model=list[BatchInstallResult])
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

    return [
        BatchInstallResult(
            slug=result["slug"],
            success=result["success"],
            error=result.get("error")
        )
        for result in results
    ]