"""Skills 系统 - 业务逻辑层（Service）

v2 要点：
- skill 是目录（dir_path），SKILL.md 正文按需读取（渐进式披露）
- 目录导入：copytree → 临时目录 → 原子 rename，失败回滚（参考 Yuxi service.py:680-717）
- 依赖展开：分支内 stack 判环 + 全局 seen 去重（菱形依赖不再误报为环）
- 语义匹配：委托 SkillMatcher（embedding 向量召回，未配置/失败回退 jieba 关键词）
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Optional

from loguru import logger

from app.skills.matching import SkillMatcher
from app.skills.models import (
    ExpandedSkills,
    Skill,
    SkillContent,
    SkillSourceType,
)
from app.skills.repository import SkillRepository


class SkillService:
    """Skills 业务逻辑：加载、解析、依赖展开、目录导入"""

    def __init__(
        self,
        repository: Optional[SkillRepository] = None,
        save_dir: Optional[str | Path] = None,
        matcher: Optional[SkillMatcher] = None,
    ):
        """
        Args:
            repository: 数据访问层（当前为内存实现）
            save_dir: 用户/远程 skills 的落盘根目录（None=不落盘，仅内存）
            matcher: 语义匹配器（None=首次匹配时按全局 settings 惰性构建；测试可注入假 embed）
        """
        self.repository = repository
        self.save_dir = Path(save_dir) if save_dir else None
        self._builtin_skills_cache: dict[str, Skill] = {}
        self._matcher = matcher

    # ========== 加载 ==========

    async def load_builtin_skills(self, builtin_dir: str | Path) -> list[Skill]:
        """加载内置 Skills（从文件系统，每个子目录一个 skill）"""
        builtin_path = Path(builtin_dir)
        if not builtin_path.exists():
            logger.warning(f"内置 skills 目录不存在: {builtin_dir}")
            return []

        skills = []
        for skill_dir in sorted(builtin_path.iterdir()):
            if not skill_dir.is_dir():
                continue

            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                logger.debug(f"跳过无 SKILL.md 的目录: {skill_dir.name}")
                continue

            try:
                skill = self._load_skill_from_dir(skill_dir)
                skill.source_type = SkillSourceType.BUILTIN
                skill.user_id = None  # 系统内置
                skills.append(skill)
                self._builtin_skills_cache[skill.slug] = skill
                logger.info(f"加载内置 skill: {skill.slug}")
            except Exception as e:
                logger.error(f"加载 skill 失败 {skill_dir.name}: {e}")

        return skills

    def _load_skill_from_dir(self, skill_dir: Path) -> Skill:
        """从目录加载 skill（读取根 SKILL.md）"""
        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        parsed = SkillContent.parse(content)

        return Skill(
            slug=parsed.frontmatter.slug,
            name=parsed.frontmatter.name,
            description=parsed.frontmatter.description,
            content=content,
            dir_path=str(skill_dir),
            enabled=True,
        )

    # ========== 查询 ==========

    async def get_skill(self, slug: str) -> Optional[Skill]:
        """获取 skill（优先内置缓存，其次数据库）"""
        if slug in self._builtin_skills_cache:
            return self._builtin_skills_cache[slug]

        if self.repository:
            skill = await self.repository.get_by_slug(slug)
            if skill:
                return skill

        return None

    async def get_skill_body(self, slug: str) -> Optional[str]:
        """读取 skill 的完整 SKILL.md 正文（渐进式披露的第二阶段）

        优先从目录读最新内容，无目录时退回 content 缓存。
        """
        skill = await self.get_skill(slug)
        if not skill or not skill.enabled:
            return None

        if skill.dir_path:
            skill_file = Path(skill.dir_path) / "SKILL.md"
            if skill_file.exists():
                return skill_file.read_text(encoding="utf-8")

        return skill.content or None

    async def list_skills(self, enabled_only: bool = True, user_id: Optional[int] = None) -> list[Skill]:
        """列表查询 skills"""
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

    # ========== 依赖展开 ==========

    async def expand_dependencies(self, skill_slugs: list[str], max_depth: int = 10) -> ExpandedSkills:
        """展开 skills 依赖（递归，仅对 skills 边递归）

        环检测：分支内 stack 判环（真环告警跳过）；
        全局 seen 去重（菱形依赖静默去重，不误报为环）。
        """
        expanded = ExpandedSkills()
        seen: set[str] = set()

        async def visit(slug: str, stack: tuple[str, ...]) -> None:
            if slug in stack:
                cycle = " -> ".join(stack + (slug,))
                logger.warning(f"检测到循环依赖，跳过: {cycle}")
                return
            if slug in seen:
                return  # 菱形依赖：已展开过，静默去重
            if len(stack) >= max_depth:
                logger.warning(f"超过最大依赖深度 {max_depth}，跳过: {slug}")
                return

            skill = await self.get_skill(slug)
            if not skill:
                logger.warning(f"Skill 不存在: {slug}")
                return
            if not skill.enabled:
                logger.info(f"Skill 已禁用: {slug}")
                return

            seen.add(slug)
            expanded.add_skill(skill)

            for dep_slug in skill.get_skills():
                await visit(dep_slug, stack + (slug,))

        for slug in skill_slugs:
            await visit(slug, ())

        expanded.deduplicate()
        return expanded

    # ========== 语义匹配（委托 SkillMatcher） ==========

    def _get_matcher(self) -> SkillMatcher:
        """惰性构建语义匹配器：未注入则按全局 settings 判定策略（auto）"""
        if self._matcher is None:
            from app.core.settings import get_settings

            self._matcher = SkillMatcher(settings=get_settings())
        return self._matcher

    def _invalidate_match_cache(self, slug: str) -> None:
        """技能增/删/改后失效其匹配向量缓存。

        matcher 未构建则无缓存可失效（首次匹配时才建、届时缓存本就为空），直接跳过。
        """
        if self._matcher is not None:
            self._matcher.invalidate(slug)

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """jieba 分词（中英文皆可），过滤单字符标点

        分词实现已迁入 matching.py，这里委托保持单一口径；
        app/text2sql/examples.py 仍按 `SkillService._tokenize` 调用，签名与语义不变。
        """
        return SkillMatcher._tokenize(text)

    async def match_skills_by_query(
        self, query: str, candidate_slugs: Optional[list[str]] = None, top_k: int = 3
    ) -> list[Skill]:
        """根据用户查询匹配 skills

        候选集解析（candidate_slugs 或全部启用）后，打分/排序委托 SkillMatcher：
        embedding 配置可用则向量召回，否则（或调用失败）回退 jieba 关键词。
        签名与返回值保持与 v1 一致（middleware 的 auto_match 依赖此契约）。
        """
        if candidate_slugs:
            skills = []
            for slug in candidate_slugs:
                skill = await self.get_skill(slug)
                if skill and skill.enabled:
                    skills.append(skill)
        else:
            skills = await self.list_skills(enabled_only=True)

        return await self._get_matcher().match(query, skills, top_k=top_k)

    # ========== 目录导入（v2 核心） ==========

    async def import_skill_dir(
        self,
        source_dir: str | Path,
        source_type: SkillSourceType = SkillSourceType.UPLOAD,
        user_id: Optional[int] = None,
        enabled: bool = True,
    ) -> Skill:
        """导入一个 skill 目录（整树复制，含 scripts/ 等随附文件）

        流程：解析校验 → copytree 到临时目录 → 原子 rename 到最终位置 →
        入库；入库失败则删除目录回滚。
        """
        source_path = Path(source_dir)
        skill_file = source_path / "SKILL.md"
        if not skill_file.exists():
            raise ValueError(f"目录中没有 SKILL.md: {source_dir}")

        content = skill_file.read_text(encoding="utf-8")
        parsed = SkillContent.parse(content)
        slug = parsed.frontmatter.slug

        existing = await self.get_skill(slug)
        if existing:
            raise ValueError(f"Skill slug 已存在: {slug}")

        if self.save_dir is None:
            raise ValueError("SkillService 未配置 save_dir，无法落盘导入目录")

        skills_root = self.save_dir / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        final_dir = skills_root / slug
        if final_dir.exists():
            raise ValueError(f"目标目录已存在: {final_dir}")

        # copytree → 临时目录 → 原子 rename
        tmp_dir = skills_root / f".{slug}.tmp-{uuid.uuid4().hex[:8]}"
        try:
            shutil.copytree(source_path, tmp_dir, symlinks=False)
            tmp_dir.rename(final_dir)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        skill = Skill(
            slug=slug,
            name=parsed.frontmatter.name,
            description=parsed.frontmatter.description,
            content=content,
            dir_path=str(final_dir),
            source_type=source_type,
            enabled=enabled,
            user_id=user_id,
        )

        try:
            if self.repository:
                skill = await self.repository.create(skill)
        except Exception:
            # 入库失败回滚目录，保持 FS/存储一致
            shutil.rmtree(final_dir, ignore_errors=True)
            raise

        # slug 若曾被删后重建，清掉可能残留的旧向量
        self._invalidate_match_cache(slug)
        logger.info(f"导入 skill 目录成功: {slug} -> {final_dir}")
        return skill

    # ========== 增删改 ==========

    async def create_skill(self, content: str, user_id: Optional[int] = None) -> Skill:
        """创建 skill（从 SKILL.md 内容；配置了 save_dir 时落盘为目录）"""
        parsed = SkillContent.parse(content)
        slug = parsed.frontmatter.slug

        existing = await self.get_skill(slug)
        if existing:
            raise ValueError(f"Skill slug 已存在: {slug}")

        dir_path: Optional[str] = None
        if self.save_dir is not None:
            skill_dir = self.save_dir / "skills" / slug
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
            dir_path = str(skill_dir)

        skill = Skill(
            slug=slug,
            name=parsed.frontmatter.name,
            description=parsed.frontmatter.description,
            content=content,
            dir_path=dir_path,
            source_type=SkillSourceType.UPLOAD,
            enabled=True,
            user_id=user_id,
        )

        if self.repository:
            skill = await self.repository.create(skill)

        self._invalidate_match_cache(slug)
        return skill

    async def update_skill(self, slug: str, content: str, user_id: Optional[int] = None) -> Skill:
        """更新 skill（重写根 SKILL.md）"""
        existing = await self.get_skill(slug)
        if not existing:
            raise ValueError(f"Skill 不存在: {slug}")

        if existing.source_type == SkillSourceType.BUILTIN:
            raise ValueError("内置 skill 不能通过 API 修改")

        # 权限检查：只有创建者能更新
        if existing.user_id and existing.user_id != user_id:
            raise PermissionError(f"无权限更新 skill: {slug}")

        parsed = SkillContent.parse(content)
        if parsed.frontmatter.slug != slug:
            raise ValueError(f"frontmatter slug ({parsed.frontmatter.slug}) 与目标 skill ({slug}) 不一致")

        existing.name = parsed.frontmatter.name
        existing.description = parsed.frontmatter.description
        existing.content = content
        existing._parsed = None  # 失效解析缓存

        if existing.dir_path:
            (Path(existing.dir_path) / "SKILL.md").write_text(content, encoding="utf-8")

        if self.repository:
            existing = await self.repository.update(existing)

        # name/description 可能已变，失效旧向量，下次匹配按新文本重算
        self._invalidate_match_cache(slug)
        return existing

    async def delete_skill(self, slug: str, user_id: Optional[int] = None) -> bool:
        """删除 skill（含落盘目录）"""
        existing = await self.get_skill(slug)
        if not existing:
            return False

        if existing.source_type == SkillSourceType.BUILTIN:
            raise ValueError("内置 skill 不能删除")

        if existing.user_id and existing.user_id != user_id:
            raise PermissionError(f"无权限删除 skill: {slug}")

        deleted = True
        if self.repository:
            deleted = await self.repository.delete(slug)

        # 仅删除 save_dir 管辖内的目录（内置目录不在其中）
        if deleted and existing.dir_path and self.save_dir is not None:
            dir_path = Path(existing.dir_path)
            try:
                dir_path.resolve().relative_to((self.save_dir / "skills").resolve())
                shutil.rmtree(dir_path, ignore_errors=True)
            except ValueError:
                logger.warning(f"skill 目录不在 save_dir 内，跳过删除: {dir_path}")

        if deleted:
            self._invalidate_match_cache(slug)
        return deleted
