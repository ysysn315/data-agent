"""持久化层测试（SQLAlchemy 2.0 async）

覆盖：
- 四表 CRUD（技能仓储走完整 InMemory 契约；mcp/示例/术语仓储走增删查 + 整表替换）
- 重启持久性（同文件重开 engine，数据仍在）
- skills 表不存正文（无 content 列；正文从 dir_path 现读）
- JSON→DB 一次性迁移（MCP 注册表 + 示例/术语，二次启动不重复迁移）
- 种子写入幂等（表空写种子；二次启动不重复）

约定：
- 直连仓储的用例用 async，每例 tmp_path 独立 sqlite 文件、独立 engine。
- 同步门面（MCPService/ExampleStore/TermStore 的 DB 版）的用例用 sync + 注入 runner
  （持久化事件循环），既隔离又真正跑到门面里的迁移/种子分支。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app.db.engine import create_engine_and_sessionmaker, init_db
from app.db.repositories import (
    MCPRepository,
    SqlAlchemySkillRepository,
    SQLExampleRepository,
    TerminologyRepository,
)
from app.mcp.models import MCPServer
from app.mcp.service import MCPService
from app.skills.models import Skill, SkillSourceType
from app.text2sql.examples import SEED_EXAMPLES, ExampleStore
from app.text2sql.terminology import SEED_TERMS, TermStore


def _db_url(tmp_path: Path, name: str = "app.db") -> str:
    return f"sqlite+aiosqlite:///{tmp_path / name}"


def _make_skill_dir(root: Path, slug: str, body: str = "正文只在文件系统。") -> Path:
    """在文件系统落一个合法 skill 目录，返回目录路径。"""
    skill_dir = root / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {slug}\nslug: {slug}\ndescription: 技能 {slug}\n---\n\n# {slug}\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def _table_columns(db_file: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_file)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _make_skill(root: Path, slug: str, **kw) -> Skill:
    skill_dir = _make_skill_dir(root, slug)
    defaults = dict(
        slug=slug,
        name=f"技能{slug}",
        description=f"技能 {slug} 的描述",
        content=(skill_dir / "SKILL.md").read_text(encoding="utf-8"),
        dir_path=str(skill_dir),
        source_type=SkillSourceType.UPLOAD,
        enabled=True,
    )
    defaults.update(kw)
    return Skill(**defaults)


# ========== 技能仓储：CRUD（完整 InMemory 契约） ==========


async def test_skill_repo_full_crud(tmp_path):
    engine, sm = create_engine_and_sessionmaker(_db_url(tmp_path))
    await init_db(engine)
    try:
        repo = SqlAlchemySkillRepository(sm)

        # create：回填 id/时间戳
        created = await repo.create(_make_skill(tmp_path, "alpha", user_id=1))
        assert created.id == 1
        assert created.created_at is not None and created.updated_at is not None
        assert await repo.count() == 1
        assert await repo.exists("alpha") is True

        # slug 重复应拒绝（与 InMemory 一致）
        with pytest.raises(ValueError, match="已存在"):
            await repo.create(_make_skill(tmp_path, "alpha"))

        # get_by_slug / get_by_id
        got = await repo.get_by_slug("alpha")
        assert got.id == 1 and got.source_type == SkillSourceType.UPLOAD
        assert (await repo.get_by_id(1)).slug == "alpha"
        assert await repo.get_by_slug("不存在") is None

        # 第二个技能（另一个用户 + builtin 来源，测过滤）
        await repo.create(_make_skill(tmp_path, "beta", user_id=2, source_type=SkillSourceType.REMOTE))

        # list_all + 过滤
        assert len(await repo.list_all()) == 2
        assert len(await repo.list_all(source_type=SkillSourceType.REMOTE)) == 1

        # enable/disable + list_enabled + enabled_only
        assert await repo.disable("beta") is True
        assert (await repo.get_by_slug("beta")).enabled is False
        assert len(await repo.list_enabled()) == 1
        assert await repo.enable("beta") is True
        assert len(await repo.list_enabled()) == 2
        assert await repo.enable("幽灵") is False

        # list_accessible_by_user：全局(None) + 自己；他人不可见
        await repo.create(_make_skill(tmp_path, "global", user_id=None))
        acc = await repo.list_accessible_by_user(1)
        slugs = {s.slug for s in acc}
        assert "global" in slugs and "alpha" in slugs and "beta" not in slugs

        # update：改 description/enabled 落库
        got = await repo.get_by_slug("alpha")
        got.description = "改过的描述"
        got.enabled = False
        updated = await repo.update(got)
        assert updated.description == "改过的描述"
        assert (await repo.get_by_slug("alpha")).description == "改过的描述"
        with pytest.raises(ValueError, match="不存在"):
            await repo.update(_make_skill(tmp_path, "没这个"))

        # delete / clear
        assert await repo.delete("beta") is True
        assert await repo.delete("beta") is False
        await repo.clear()
        assert await repo.count() == 0
    finally:
        await engine.dispose()


# ========== 技能表：不存正文，正文从 dir_path 现读 ==========


async def test_skills_table_stores_no_content(tmp_path):
    url = _db_url(tmp_path)
    engine, sm = create_engine_and_sessionmaker(url)
    await init_db(engine)
    try:
        repo = SqlAlchemySkillRepository(sm)
        await repo.create(_make_skill(tmp_path, "alpha", user_id=1))
    finally:
        await engine.dispose()

    # 断言 skills 表根本没有 content 列（正文不入库）
    cols = _table_columns(tmp_path / "app.db", "skills")
    assert "content" not in cols
    assert {"slug", "name", "description", "dir_path", "source_type", "enabled"} <= cols

    # 重新打开，正文应从 dir_path/SKILL.md 现读回来（可被 SkillContent 解析）
    engine2, sm2 = create_engine_and_sessionmaker(url)
    try:
        got = await SqlAlchemySkillRepository(sm2).get_by_slug("alpha")
        assert "正文只在文件系统" in got.content
        assert got.parsed.frontmatter.slug == "alpha"

        # 目录被删后，正文读为空串（数据库里本就没有备份），元数据仍在
        import shutil

        shutil.rmtree(got.dir_path)
        got2 = await SqlAlchemySkillRepository(sm2).get_by_slug("alpha")
        assert got2.content == ""
        assert got2.slug == "alpha"
    finally:
        await engine2.dispose()


# ========== 重启持久性：同文件重开 engine，数据仍在 ==========


async def test_restart_persistence(tmp_path):
    url = _db_url(tmp_path)

    engine, sm = create_engine_and_sessionmaker(url)
    await init_db(engine)
    try:
        repo = SqlAlchemySkillRepository(sm)
        await repo.create(_make_skill(tmp_path, "alpha", user_id=1))
        await MCPRepository(sm).upsert(
            MCPServer(slug="chart", transport="stdio", command="python", args=["-c", "pass"])
        )
        await SQLExampleRepository(sm).upsert(
            {"id": "ex1", "question": "各州客户数", "sql": "SELECT 1", "verified": True}
        )
        await TerminologyRepository(sm).upsert(
            {"term": "GMV", "synonyms": ["成交额"], "definition": "成交总额", "sql_hint": "SUM(price)"}
        )
    finally:
        await engine.dispose()

    # 全新 engine 打开同一文件
    engine2, sm2 = create_engine_and_sessionmaker(url)
    try:
        assert (await SqlAlchemySkillRepository(sm2).get_by_slug("alpha")).id == 1
        assert (await MCPRepository(sm2).get("chart")).command == "python"
        assert (await SQLExampleRepository(sm2).list_all())[0]["question"] == "各州客户数"
        assert (await TerminologyRepository(sm2).list_all())[0]["term"] == "GMV"
    finally:
        await engine2.dispose()


# ========== MCP / 示例 / 术语仓储：CRUD + 整表替换 ==========


async def test_mcp_repo_crud(tmp_path):
    engine, sm = create_engine_and_sessionmaker(_db_url(tmp_path))
    await init_db(engine)
    try:
        repo = MCPRepository(sm)
        await repo.upsert(
            MCPServer(
                slug="chart",
                name="图表",
                transport="streamable_http",
                url="http://localhost:1122/mcp",
                headers={"A": "b"},
                timeout=5,
                disabled_tools=["danger"],
            )
        )
        assert await repo.count() == 1
        got = await repo.get("chart")
        assert got.url == "http://localhost:1122/mcp" and got.headers == {"A": "b"}
        assert got.disabled_tools == ["danger"]

        # upsert 覆盖
        await repo.upsert(MCPServer(slug="chart", transport="stdio", command="python"))
        assert (await repo.get("chart")).transport == "stdio"
        assert await repo.count() == 1

        # replace_all（整表替换）
        await repo.replace_all(
            [
                MCPServer(slug="a", transport="stdio", command="x"),
                MCPServer(slug="b", transport="stdio", command="y"),
            ]
        )
        assert await repo.count() == 2 and await repo.get("chart") is None
        assert await repo.delete("a") is True and await repo.delete("a") is False
    finally:
        await engine.dispose()


async def test_example_and_term_repo_crud(tmp_path):
    engine, sm = create_engine_and_sessionmaker(_db_url(tmp_path))
    await init_db(engine)
    try:
        ex = SQLExampleRepository(sm)
        await ex.upsert({"id": "e1", "question": "q1", "sql": "SELECT 1", "verified": True})
        await ex.upsert({"id": "e2", "question": "q2", "sql": "SELECT 2", "verified": False})
        assert await ex.count() == 2
        await ex.upsert({"id": "e1", "question": "q1改", "sql": "SELECT 11", "verified": True})
        rows = await ex.list_all()
        assert len(rows) == 2 and any(r["question"] == "q1改" for r in rows)
        assert await ex.delete("e2") is True
        await ex.replace_all([{"id": "z", "question": "zz", "sql": "SELECT 9", "verified": True}])
        assert [r["id"] for r in await ex.list_all()] == ["z"]

        tm = TerminologyRepository(sm)
        await tm.upsert({"term": "GMV", "synonyms": ["成交额"], "definition": "d", "sql_hint": "h"})
        await tm.upsert({"term": "复购率", "synonyms": [], "definition": "d2", "sql_hint": None})
        assert await tm.count() == 2
        got = {t["term"]: t for t in await tm.list_all()}
        assert got["GMV"]["synonyms"] == ["成交额"] and got["复购率"]["sql_hint"] is None
        assert await tm.delete("GMV") is True
        await tm.replace_all([{"term": "客单价", "synonyms": [], "definition": "x", "sql_hint": None}])
        assert [t["term"] for t in await tm.list_all()] == ["客单价"]
    finally:
        await engine.dispose()


# ========== 同步门面：JSON→DB 一次性迁移 + 种子幂等 ==========


@pytest.fixture
def sync_db(tmp_path):
    """同步门面用：tmp sqlite + 持久化事件循环 runner（模拟 app.db.run_sync 的桥）。"""
    loop = asyncio.new_event_loop()

    def run(coro):
        return loop.run_until_complete(coro)

    engine, sm = create_engine_and_sessionmaker(_db_url(tmp_path))
    run(init_db(engine))
    try:
        yield sm, run, tmp_path
    finally:
        run(engine.dispose())
        loop.close()


def test_mcp_json_to_db_migration_once(sync_db):
    sm, run, tmp_path = sync_db
    repo = MCPRepository(sm)

    # 历史 JSON 注册表
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text(
        json.dumps(
            {
                "servers": [
                    MCPServer(slug="chart", transport="stdio", command="python").model_dump(),
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 首次启动：表空 + 有历史 JSON → 一次性迁移入库
    svc = MCPService(config_path=cfg, repo=repo, runner=run)
    assert svc.get_server("chart") is not None
    assert run(repo.count()) == 1

    # 之后 CRUD 落 DB
    svc.create_server(MCPServer(slug="extra", transport="stdio", command="node"))
    assert run(repo.count()) == 2

    # 二次启动：表非空 → 不再迁移（幂等），从 DB 读回全部
    svc2 = MCPService(config_path=cfg, repo=repo, runner=run)
    assert run(repo.count()) == 2
    assert {s.slug for s in svc2.list_servers()} == {"chart", "extra"}


def test_example_store_seed_and_migration(sync_db):
    sm, run, tmp_path = sync_db
    repo = SQLExampleRepository(sm)

    # 表空 + 无历史 JSON → 写种子
    store = ExampleStore(tmp_path / "sql_examples.json", repo=repo, runner=run)
    assert len(store.list()) == len(SEED_EXAMPLES)
    assert run(repo.count()) == len(SEED_EXAMPLES)

    # 反馈入库落 DB
    store.add("每个卖家的销售额", "SELECT seller_id, SUM(price) FROM order_items GROUP BY seller_id")
    assert run(repo.count()) == len(SEED_EXAMPLES) + 1

    # 二次启动：表非空 → 不重复灌种，从 DB 读回
    store2 = ExampleStore(tmp_path / "sql_examples.json", repo=repo, runner=run)
    assert run(repo.count()) == len(SEED_EXAMPLES) + 1
    assert any(r["question"] == "每个卖家的销售额" for r in store2.list())
    # 检索仍能命中（内存缓存 + jieba）
    assert store2.search("各州客户数量分布") != []


def test_term_store_json_to_db_migration(sync_db):
    sm, run, tmp_path = sync_db
    repo = TerminologyRepository(sm)

    # 历史 JSON 术语库（非种子内容），表空 → 迁移这批而非灌种
    path = tmp_path / "terminology.json"
    path.write_text(
        json.dumps(
            {
                "terms": [
                    {"term": "动销率", "synonyms": ["动销"], "definition": "有销量商品占比", "sql_hint": None},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = TermStore(path, repo=repo, runner=run)
    assert run(repo.count()) == 1
    assert store.match("动销怎么样")[0]["term"] == "动销率"
    # 未灌入种子（因为迁移优先于种子）
    assert all(t["term"] != "GMV" for t in store.list())

    # 二次启动：不重复迁移
    store2 = TermStore(path, repo=repo, runner=run)
    assert run(repo.count()) == 1
    assert {t["term"] for t in store2.list()} == {"动销率"}


def test_term_store_seed_when_empty(sync_db):
    sm, run, tmp_path = sync_db
    repo = TerminologyRepository(sm)
    # 表空 + 无历史 JSON → 灌种子；二次启动幂等
    store = TermStore(tmp_path / "terminology.json", repo=repo, runner=run)
    assert {t["term"] for t in store.list()} == {t["term"] for t in SEED_TERMS}
    assert run(repo.count()) == len(SEED_TERMS)
    TermStore(tmp_path / "terminology.json", repo=repo, runner=run)
    assert run(repo.count()) == len(SEED_TERMS)
