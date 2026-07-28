"""Skills 系统 - 模块入口"""

from app.skills.middleware import SkillsMiddleware, SkillsState
from app.skills.models import (
    ExpandedSkills,
    Skill,
    SkillContent,
    SkillFrontmatter,
    SkillSourceType,
    SkillStatus,
)
from app.skills.remote_install import (
    RemoteInstallError,
    install_remote_skill,
    install_remote_skills_batch,
    list_remote_skills,
)
from app.skills.repository import SkillRepository
from app.skills.service import SkillService

__all__ = [
    # 数据模型
    "Skill",
    "SkillContent",
    "SkillFrontmatter",
    "ExpandedSkills",
    "SkillSourceType",
    "SkillStatus",
    # 数据访问层
    "SkillRepository",
    # 业务逻辑层
    "SkillService",
    # Middleware
    "SkillsMiddleware",
    "SkillsState",
    # 远程安装
    "RemoteInstallError",
    "install_remote_skill",
    "install_remote_skills_batch",
    "list_remote_skills",
]
