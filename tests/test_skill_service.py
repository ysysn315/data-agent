"""SkillService 测试：加载 / 依赖展开（菱形与环）/ 中文匹配 / 目录导入"""

from pathlib import Path

import pytest

from app.skills.models import SkillSourceType
from app.skills.repository import InMemorySkillRepository
from app.skills.service import SkillService
from tests.conftest import make_skill_md


async def test_load_builtin_skills(skill_service):
    skills = await skill_service.list_skills(enabled_only=True)
    slugs = {s.slug for s in skills}
    assert {"schema-retrieval", "sql-generation", "data-visualization", "sqlite-query"} <= slugs
    # v2：内置 skill 必须携带目录路径
    for skill in skills:
        assert skill.dir_path, f"{skill.slug} 缺少 dir_path"


async def test_get_skill_body(skill_service):
    body = await skill_service.get_skill_body("sqlite-query")
    assert body is not None
    assert "execute_sql" in body
    assert await skill_service.get_skill_body("不存在的") is None


async def test_expand_chain(skill_service):
    """链式依赖：sqlite-query -> sql-generation -> schema-retrieval"""
    expanded = await skill_service.expand_dependencies(["sqlite-query"])
    slugs = [s.slug for s in expanded.skills]
    assert slugs == ["sqlite-query", "sql-generation", "schema-retrieval"]
    assert "execute_sql" in expanded.tools


async def test_expand_diamond_no_false_cycle_warning(tmp_path, loguru_capture):
    """菱形依赖（A->B,C; B->D; C->D）：D 只出现一次，且不应告警循环依赖"""
    service = SkillService(repository=InMemorySkillRepository(), save_dir=tmp_path)
    deps_map = {
        "aa": "dependencies:\n  skills: [bb, cc]\n",
        "bb": "dependencies:\n  skills: [dd]\n",
        "cc": "dependencies:\n  skills: [dd]\n",
        "dd": "",
    }
    for slug, deps in deps_map.items():
        await service.create_skill(make_skill_md(slug, deps))

    expanded = await service.expand_dependencies(["aa"])
    slugs = [s.slug for s in expanded.skills]
    assert sorted(slugs) == ["aa", "bb", "cc", "dd"]
    assert slugs.count("dd") == 1
    assert not any("循环依赖" in r for r in loguru_capture)


async def test_expand_true_cycle_warns_and_terminates(tmp_path, loguru_capture):
    """真环（A->B->A）：应告警并终止，不死循环"""
    service = SkillService(repository=InMemorySkillRepository(), save_dir=tmp_path)
    await service.create_skill(make_skill_md("aa", "dependencies:\n  skills: [bb]\n"))
    await service.create_skill(make_skill_md("bb", "dependencies:\n  skills: [aa]\n"))

    expanded = await service.expand_dependencies(["aa"])
    slugs = [s.slug for s in expanded.skills]
    assert sorted(slugs) == ["aa", "bb"]
    assert any("循环依赖" in r for r in loguru_capture)


async def test_match_chinese_queries(skill_service):
    """中文查询必须能命中（v1 用 str.split 分词，中文全部失效）"""
    cases = {
        "查询数据库表结构": "schema-retrieval",
        "帮我生成 SQL 查询语句": "sql-generation",
        "把结果做成可视化图表": "data-visualization",
    }
    for query, expected in cases.items():
        matched = await skill_service.match_skills_by_query(query, top_k=3)
        slugs = [s.slug for s in matched]
        assert expected in slugs, f"查询 {query!r} 未命中 {expected}，实际: {slugs}"


async def test_import_skill_dir(skill_service, tmp_path):
    """整目录导入：随附文件必须一起复制"""
    src = tmp_path / "src-skill"
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text(make_skill_md("imported-skill"), encoding="utf-8")
    (src / "scripts" / "run.py").write_text("print('hi')", encoding="utf-8")

    skill = await skill_service.import_skill_dir(src, source_type=SkillSourceType.UPLOAD)
    assert skill.slug == "imported-skill"
    installed = Path(skill.dir_path)
    assert (installed / "SKILL.md").exists()
    assert (installed / "scripts" / "run.py").exists()

    # slug 冲突要报错
    with pytest.raises(ValueError, match="已存在"):
        await skill_service.import_skill_dir(src)


async def test_delete_skill_removes_dir(skill_service, tmp_path):
    src = tmp_path / "src2"
    src.mkdir()
    (src / "SKILL.md").write_text(make_skill_md("to-delete"), encoding="utf-8")
    skill = await skill_service.import_skill_dir(src, user_id=1)

    assert await skill_service.delete_skill("to-delete", user_id=1)
    assert not Path(skill.dir_path).exists()


async def test_builtin_skill_cannot_be_deleted(skill_service):
    with pytest.raises(ValueError, match="内置"):
        await skill_service.delete_skill("sqlite-query", user_id=1)
