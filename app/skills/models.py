"""Skills 系统 - 数据模型"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, field_validator


class SkillSourceType(str, Enum):
    """Skill 来源类型"""
    BUILTIN = "builtin"      # 系统内置
    UPLOAD = "upload"        # 用户上传
    REMOTE = "remote"        # 远程安装


class SkillStatus(str, Enum):
    """Skill 状态"""
    ENABLED = "enabled"
    DISABLED = "disabled"


# SKILL.md YAML frontmatter 正则
FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL
)


class SkillFrontmatter(BaseModel):
    """SKILL.md YAML frontmatter 解析模型"""
    name: str = Field(..., description="显示名称")
    slug: str = Field(..., description="唯一标识")
    description: str = Field(default="", description="一句话描述")
    version: str = Field(default="1.0.0", description="版本")
    author: str = Field(default="", description="作者")
    dependencies: dict[str, list[str]] = Field(
        default_factory=dict,
        description="依赖声明：tools/mcps/skills"
    )

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        """验证 slug 格式：小写字母、数字、连字符"""
        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", v):
            raise ValueError(
                "slug 必须是小写字母、数字、连字符组合，如 'schema-retrieval'"
            )
        return v


class SkillContent(BaseModel):
    """SKILL.md 完整内容解析"""
    frontmatter: SkillFrontmatter
    body: str = Field(..., description="Markdown 正文")
    raw: str = Field(..., description="原始文件内容")

    @classmethod
    def parse(cls, raw_content: str) -> "SkillContent":
        """解析 SKILL.md 文件内容"""
        match = FRONTMATTER_PATTERN.match(raw_content.strip())
        if not match:
            raise ValueError("SKILL.md 格式错误：缺少 YAML frontmatter")

        frontmatter_text, body = match.groups()

        try:
            frontmatter_dict = yaml.safe_load(frontmatter_text)
            frontmatter = SkillFrontmatter(**frontmatter_dict)
        except yaml.YAMLError as e:
            raise ValueError(f"YAML frontmatter 解析失败: {e}")
        except Exception as e:
            raise ValueError(f"frontmatter 验证失败: {e}")

        return cls(
            frontmatter=frontmatter,
            body=body.strip(),
            raw=raw_content
        )


@dataclass
class Skill:
    """Skill 领域模型（数据库实体）"""
    id: Optional[int] = None
    slug: str = ""
    name: str = ""
    description: str = ""
    content: str = ""                    # SKILL.md 完整内容
    source_type: SkillSourceType = SkillSourceType.BUILTIN
    enabled: bool = True
    user_id: Optional[int] = None        # 创建者，NULL=系统内置
    share_config: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # 解析后的内容（非数据库字段）
    _parsed: Optional[SkillContent] = None

    @property
    def parsed(self) -> SkillContent:
        """懒加载解析 SKILL.md 内容"""
        if self._parsed is None:
            self._parsed = SkillContent.parse(self.content)
        return self._parsed

    @property
    def dependencies(self) -> dict[str, list[str]]:
        """获取依赖声明"""
        return self.parsed.frontmatter.dependencies

    def get_tools(self) -> list[str]:
        """获取依赖的工具列表"""
        return self.dependencies.get("tools", [])

    def get_mcps(self) -> list[str]:
        """获取依赖的 MCP 列表"""
        return self.dependencies.get("mcps", [])

    def get_skills(self) -> list[str]:
        """获取依赖的其他 skills 列表"""
        return self.dependencies.get("skills", [])

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（API 响应）"""
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "source_type": self.source_type.value,
            "enabled": self.enabled,
            "user_id": self.user_id,
            "share_config": self.share_config,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "frontmatter": self.parsed.frontmatter.model_dump(),
            "body": self.parsed.body
        }


@dataclass
class SkillDependencyNode:
    """Skill 依赖节点（用于依赖展开）"""
    slug: str
    tools: list[str] = field(default_factory=list)
    mcps: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    depth: int = 0                        # 依赖深度（防止循环）


@dataclass
class ExpandedSkills:
    """依赖展开结果"""
    skills: list[Skill] = field(default_factory=list)           # 所有 skills（含依赖）
    tools: list[str] = field(default_factory=list)               # 所有工具（去重）
    mcps: list[str] = field(default_factory=list)                # 所有 MCP（去重）
    prompt_sections: list[str] = field(default_factory=list)     # 提示词片段

    def add_skill(self, skill: Skill) -> None:
        """添加 skill 并收集其依赖"""
        self.skills.append(skill)
        self.tools.extend(skill.get_tools())
        self.mcps.extend(skill.get_mcps())
        self.prompt_sections.append(
            f"## Skill: {skill.name}\n\n{skill.parsed.body}"
        )

    def deduplicate(self) -> None:
        """去重工具和 MCP"""
        self.tools = list(dict.fromkeys(self.tools))
        self.mcps = list(dict.fromkeys(self.mcps))

    def build_system_prompt(self) -> str:
        """构建 system message 提示词"""
        if not self.prompt_sections:
            return ""

        sections = [
            "# 可用技能（Skills）",
            "",
            "你可以使用以下技能来完成任务：",
            ""
        ]
        sections.extend(self.prompt_sections)
        return "\n".join(sections)