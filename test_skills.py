"""Skills 系统测试脚本"""
import asyncio
from pathlib import Path

from app.skills.service import SkillService
from app.skills.repository import InMemorySkillRepository


async def test_load_builtin_skills():
    """测试加载内置 Skills"""
    repo = InMemorySkillRepository()
    service = SkillService(repository=repo)

    builtin_dir = Path("app/skills/buildin")
    print(f"内置 Skills 目录: {builtin_dir.absolute()}")
    print(f"目录存在: {builtin_dir.exists()}")

    skills = await service.load_builtin_skills(builtin_dir)
    print(f"\n加载到 {len(skills)} 个内置 skills:")
    for skill in skills:
        print(f"  - {skill.slug}: {skill.name}")
        print(f"    描述: {skill.description}")
        print(f"    依赖: {skill.dependencies}")

    return service


async def test_expand_dependencies(service: SkillService):
    """测试依赖展开"""
    print("\n" + "="*50)
    print("测试依赖展开: sql-generation")
    print("="*50)

    expanded = await service.expand_dependencies(["sql-generation"])
    print(f"Skills: {[s.slug for s in expanded.skills]}")
    print(f"Tools: {expanded.tools}")
    print(f"MCPs: {expanded.mcps}")
    print(f"\nSystem Prompt 片段:")
    print(expanded.build_system_prompt()[:500])


async def test_match_skills(service: SkillService):
    """测试语义匹配"""
    print("\n" + "="*50)
    print("测试语义匹配")
    print("="*50)

    queries = [
        "查询数据库表结构",
        "生成 SQL 查询",
        "制作图表",
    ]

    for query in queries:
        matched = await service.match_skills_by_query(query, top_k=2)
        print(f"\n查询: {query}")
        for skill in matched:
            print(f"  匹配: {skill.slug} - {skill.name}")


async def main():
    print("="*50)
    print("Skills 系统测试")
    print("="*50)

    # 测试加载
    service = await test_load_builtin_skills()

    # 测试依赖展开
    await test_expand_dependencies(service)

    # 测试语义匹配
    await test_match_skills(service)

    print("\n" + "="*50)
    print("测试完成")
    print("="*50)


if __name__ == "__main__":
    asyncio.run(main())