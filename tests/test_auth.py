"""API Key 鉴权 + 工作空间隔离（F 轮）测试

覆盖：
- 开关关（demo）：/me 回占位 dev_user、写口全开（行为与鉴权落地前一致）。
- 开关开（auth）：
  * bootstrap 幂等 + 日志含 key 前缀
  * verify_api_key：合法/错误/空/禁用
  * /me 校验自身 key（合法 200 / 无 key、错 key 401）
  * admin 用户管理（建/列不回哈希/禁用）；member 建用户被 403
  * member 建技能可以、启停 403、admin 启用 200
  * 工作空间隔离：两个 workspace 各建技能互不可见、内置皆可见、admin 全见
  * 保护清单矩阵化：逐口 no-key=401 / member(admin 口)=403 / 登录口非 401-403
  * 读接口保持开放（无 key 也 200）
  * 受保护清单与真实路由一致

隔离约定：
- 鉴权服务走全局 async engine（get_sessionmaker），故用 auth_db fixture 把全局库指到
  tmp sqlite（reset_engine + ensure_initialized），用完复位；不污染其它用例。
- API 用 httpx.AsyncClient(ASGITransport)（与 test_async_tasks 同款，单事件循环，
  可在同一 loop 里 await create_user 再发请求）。ASGITransport 不触发 startup，故 bootstrap
  只在被显式 await 时发生，测试可控。
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from app.core import auth
from app.core.settings import settings
from app.main import app

BUILTIN_DIR = Path(__file__).parent.parent / "app" / "skills" / "buildin"


# ========== 公共工具 ==========

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _skill_md(slug: str) -> str:
    return f"---\nname: {slug}\nslug: {slug}\ndescription: 测试技能 {slug}\n---\n\n# {slug}\n\n正文。\n"


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    """把全局持久化 engine 指向 tmp sqlite，建表；用完复位。"""
    from app import db as dbmod

    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    dbmod.reset_engine()
    dbmod.ensure_initialized()
    yield
    dbmod.reset_engine()


@pytest.fixture
def auth_on(auth_db, monkeypatch):
    """auth_db + 打开鉴权开关。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    yield


# ========== 轻量假件（保护矩阵/读开放用，避免真实技能/MCP 副作用） ==========

class _FakeSkillService:
    repository = None

    async def list_skills(self, enabled_only: bool = True, user_id=None):
        return []

    async def get_skill(self, slug: str):
        return None

    async def create_skill(self, content: str, user_id=None):
        raise ValueError("stub")

    async def update_skill(self, slug: str, content: str, user_id=None):
        raise ValueError("stub")

    async def delete_skill(self, slug: str, user_id=None) -> bool:
        return False


class _FakeMCPService:
    def list_servers(self):
        return []

    def get_server(self, slug: str):
        return None

    def create_server(self, server):
        return server

    def update_server(self, slug: str, server):
        return server

    def delete_server(self, slug: str) -> bool:
        return False

    def set_enabled(self, slug: str, enabled: bool):
        raise ValueError("stub")

    async def test_server(self, slug: str):
        raise ValueError("stub")


class _FakeStore:
    def list(self):
        return []

    def add(self, *a, **k):
        raise ValueError("stub")

    def delete(self, *a, **k) -> bool:
        return False


def _override_all_services():
    from app.core.dependencies import (
        get_example_store,
        get_mcp_service,
        get_skill_service,
        get_term_store,
    )

    app.dependency_overrides[get_skill_service] = lambda: _FakeSkillService()
    app.dependency_overrides[get_mcp_service] = lambda: _FakeMCPService()
    app.dependency_overrides[get_example_store] = lambda: _FakeStore()
    app.dependency_overrides[get_term_store] = lambda: _FakeStore()


# ========== 1. 开关关（demo）：行为与从前一致 ==========

async def test_demo_me_returns_dev_user(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with _client() as c:
        r = await c.get("/api/auth/me")  # 无 key，demo 也放行
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == "dev_user" and body["role"] == "admin"


async def test_demo_writes_open(monkeypatch, tmp_path):
    """demo 下写口无需 key 即可写（登录守卫恒放行）。"""
    monkeypatch.setattr(settings, "auth_enabled", False)
    from app.core.dependencies import get_term_store
    from app.text2sql.terminology import TermStore

    app.dependency_overrides[get_term_store] = lambda: TermStore(tmp_path / "t.json")
    try:
        async with _client() as c:
            r = await c.post(
                "/api/terminology",
                json={"term": "演示词", "synonyms": [], "definition": "d"},
            )
            assert r.status_code == 201
    finally:
        app.dependency_overrides.clear()


async def test_demo_admin_endpoint_open(monkeypatch):
    """demo 下 admin 守卫端点（MCP 写口）无需 key 也放行。"""
    monkeypatch.setattr(settings, "auth_enabled", False)
    from app.core.dependencies import get_mcp_service

    app.dependency_overrides[get_mcp_service] = lambda: _FakeMCPService()
    try:
        async with _client() as c:
            r = await c.post(
                "/api/mcp/servers",
                json={"slug": "demo-x", "transport": "stdio", "command": "echo"},
            )
            assert r.status_code == 201
    finally:
        app.dependency_overrides.clear()


# ========== 2. bootstrap + verify ==========

async def test_bootstrap_idempotent_and_logs_key(auth_on, loguru_capture):
    key = await auth.bootstrap()
    assert key and key.startswith("da-")
    # 日志含 key 前缀（bootstrap 明文打印一次，供首次取用）
    prefix = key[:8]
    assert any(prefix in line for line in loguru_capture), "bootstrap 日志应含 key 前缀"

    # 幂等：再次调用不重复创建
    assert await auth.bootstrap() is None

    users = await auth.list_users()
    assert len(users) == 1
    assert users[0]["username"] == "admin" and users[0]["role"] == "admin"
    assert users[0]["api_key_prefix"] == prefix

    # 默认工作空间已建
    from app.db import get_sessionmaker
    from app.db.repositories import WorkspaceRepository

    ws = await WorkspaceRepository(get_sessionmaker()).get_by_slug("default")
    assert ws is not None


async def test_bootstrap_skipped_when_disabled(auth_db, monkeypatch):
    """auth_enabled=False 时 bootstrap 直接返回 None（不建用户）。"""
    monkeypatch.setattr(settings, "auth_enabled", False)
    assert await auth.bootstrap() is None
    assert await auth.list_users() == []


async def test_verify_api_key(auth_on):
    created = await auth.create_user("alice", "member", "default")
    key = created["api_key"]

    u = await auth.verify_api_key(key)
    assert u is not None
    assert u["username"] == "alice" and u["role"] == "member"
    assert u["workspace_id"] == created["workspace_id"]
    assert "api_key_hash" not in u  # 不外泄哈希

    # 错误 / 空 key
    assert await auth.verify_api_key("da-" + "0" * 32) is None
    assert await auth.verify_api_key("") is None

    # 禁用后失效
    await auth.disable_user(created["id"])
    assert await auth.verify_api_key(key) is None


async def test_api_key_only_hash_stored(auth_on):
    """库里只存 sha256，不存明文；prefix 为明文前 8 位。"""
    created = await auth.create_user("bob", "member", "default")
    key = created["api_key"]
    assert created["api_key_prefix"] == key[:8]

    from app.db import get_sessionmaker
    from app.db.models import UserModel
    from sqlalchemy import select

    async with get_sessionmaker()() as session:
        row = (await session.execute(select(UserModel).where(UserModel.username == "bob"))).scalar_one()
        assert row.api_key_hash == auth.hash_api_key(key)
        assert key not in row.api_key_hash  # 明文不落库
        assert len(row.api_key_hash) == 64


# ========== 3. /me ==========

async def test_me_requires_valid_key(auth_on):
    admin = await auth.create_user("root", "admin", "default")
    async with _client() as c:
        r = await c.get("/api/auth/me", headers=_bearer(admin["api_key"]))
        assert r.status_code == 200
        assert r.json()["username"] == "root" and r.json()["role"] == "admin"

        assert (await c.get("/api/auth/me")).status_code == 401  # 无 key
        assert (await c.get("/api/auth/me", headers=_bearer("da-bad"))).status_code == 401


# ========== 4. admin 用户管理 ==========

async def test_admin_user_management(auth_on):
    admin = await auth.create_user("root", "admin", "default")
    member = await auth.create_user("m1", "member", "default")
    async with _client() as c:
        # admin 建用户 -> 201，明文 key 只此一次
        r = await c.post(
            "/api/auth/users",
            headers=_bearer(admin["api_key"]),
            json={"username": "u2", "role": "member", "workspace": "default"},
        )
        assert r.status_code == 201
        assert r.json()["api_key"].startswith("da-")

        # member 建用户 -> 403
        r2 = await c.post(
            "/api/auth/users", headers=_bearer(member["api_key"]), json={"username": "u3"}
        )
        assert r2.status_code == 403

        # 无 key -> 401
        assert (await c.post("/api/auth/users", json={"username": "u4"})).status_code == 401

        # admin 列表不回哈希/明文
        r3 = await c.get("/api/auth/users", headers=_bearer(admin["api_key"]))
        assert r3.status_code == 200
        assert all("api_key_hash" not in u and "api_key" not in u for u in r3.json())
        assert {u["username"] for u in r3.json()} >= {"root", "m1", "u2"}

        # 禁用 member
        rd = await c.post(
            f"/api/auth/users/{member['id']}/disable", headers=_bearer(admin["api_key"])
        )
        assert rd.status_code == 200 and rd.json()["enabled"] is False

        # 禁用后 member key 失效
        assert (await c.get("/api/auth/me", headers=_bearer(member["api_key"]))).status_code == 401

        # 禁用不存在用户 -> 404
        assert (
            await c.post("/api/auth/users/999999/disable", headers=_bearer(admin["api_key"]))
        ).status_code == 404


# ========== 5. member 建技能可以、启停 403、admin 启用 200 ==========

async def _real_skill_service(save_root: Path):
    from app.skills.repository import InMemorySkillRepository
    from app.skills.service import SkillService

    svc = SkillService(repository=InMemorySkillRepository(), save_dir=save_root)
    await svc.load_builtin_skills(BUILTIN_DIR)
    return svc


async def test_member_create_skill_but_toggle_forbidden(auth_on, tmp_path):
    from app.core.dependencies import get_skill_service

    svc = await _real_skill_service(tmp_path / "saves")
    admin = await auth.create_user("root", "admin", "default")
    member = await auth.create_user("m1", "member", "default")

    app.dependency_overrides[get_skill_service] = lambda: svc
    try:
        async with _client() as c:
            # member 建技能 -> 201
            r = await c.post(
                "/api/skills", headers=_bearer(member["api_key"]), json={"content": _skill_md("mine")}
            )
            assert r.status_code == 201

            # member 启停 -> 403（启停需 admin）
            assert (
                await c.post("/api/skills/mine/enable", headers=_bearer(member["api_key"]))
            ).status_code == 403
            assert (
                await c.post("/api/skills/mine/disable", headers=_bearer(member["api_key"]))
            ).status_code == 403

            # admin 启用 -> 200
            assert (
                await c.post("/api/skills/mine/enable", headers=_bearer(admin["api_key"]))
            ).status_code == 200
    finally:
        app.dependency_overrides.clear()


# ========== 6. 工作空间隔离 ==========

async def test_workspace_isolation(auth_on, tmp_path):
    from app.core.dependencies import get_skill_service

    svc = await _real_skill_service(tmp_path / "saves")
    admin = await auth.create_user("root", "admin", "default")
    a = await auth.create_user("ua", "member", "alpha")
    b = await auth.create_user("ub", "member", "beta")

    app.dependency_overrides[get_skill_service] = lambda: svc
    try:
        async with _client() as c:
            assert (
                await c.post("/api/skills", headers=_bearer(a["api_key"]), json={"content": _skill_md("alpha-skill")})
            ).status_code == 201
            assert (
                await c.post("/api/skills", headers=_bearer(b["api_key"]), json={"content": _skill_md("beta-skill")})
            ).status_code == 201

            la = (await c.get("/api/skills", headers=_bearer(a["api_key"]))).json()
            slugs_a = {s["slug"] for s in la}
            assert "alpha-skill" in slugs_a and "beta-skill" not in slugs_a
            assert any(s["source_type"] == "builtin" for s in la)  # 内置可见

            lb = (await c.get("/api/skills", headers=_bearer(b["api_key"]))).json()
            slugs_b = {s["slug"] for s in lb}
            assert "beta-skill" in slugs_b and "alpha-skill" not in slugs_b
            assert any(s["source_type"] == "builtin" for s in lb)

            # admin 全见
            ladmin = (await c.get("/api/skills", headers=_bearer(admin["api_key"]))).json()
            slugs_admin = {s["slug"] for s in ladmin}
            assert {"alpha-skill", "beta-skill"} <= slugs_admin
    finally:
        app.dependency_overrides.clear()


# ========== 7. 保护清单矩阵化断言（逐口 401/403/放行） ==========

async def test_protection_matrix(auth_on):
    admin = await auth.create_user("root", "admin", "default")
    member = await auth.create_user("m1", "member", "default")
    _override_all_services()
    try:
        async with _client() as c:
            for method, path, level in auth.PROTECTED_ENDPOINTS:
                url = re.sub(r"\{[^}]+\}", "nope", path)
                tag = f"{method} {path} ({level})"

                # 无 key -> 401
                r0 = await c.request(method, url)
                assert r0.status_code == 401, f"{tag} 无 key 应 401，实得 {r0.status_code}"

                # member：admin 口 -> 403；登录口 -> 放行（非 401/403）
                rm = await c.request(method, url, headers=_bearer(member["api_key"]))
                if level == "admin":
                    assert rm.status_code == 403, f"{tag} member 应 403，实得 {rm.status_code}"
                else:
                    assert rm.status_code not in (401, 403), f"{tag} member 登录口不应被拦，实得 {rm.status_code}"

                # admin：一律放行（非 401/403）
                ra = await c.request(method, url, headers=_bearer(admin["api_key"]))
                assert ra.status_code not in (401, 403), f"{tag} admin 不应被拦，实得 {ra.status_code}"
    finally:
        app.dependency_overrides.clear()


async def test_reads_open_in_auth_mode(auth_on):
    """读接口在 auth 模式下无 key 也开放（demo 观感）。"""
    _override_all_services()
    try:
        async with _client() as c:
            for url in ["/api/skills", "/api/mcp/servers", "/api/sql-examples", "/api/terminology"]:
                r = await c.get(url)
                assert r.status_code == 200, f"读口 {url} 应开放，实得 {r.status_code}"
    finally:
        app.dependency_overrides.clear()


def test_protected_registry_matches_routes():
    """受保护清单里每个端点都真实注册（清单不漂移）。

    用公开的 OpenAPI schema 而非内部 route 对象枚举——本版 FastAPI 用 _IncludedRouter
    惰性挂载子路由，app.routes 不再是拍平的 APIRoute，openapi()["paths"] 才是稳定口径。
    """
    schema = app.openapi()
    registered = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
    }
    for method, path, level in auth.PROTECTED_ENDPOINTS:
        assert level in ("login", "admin"), f"非法级别: {level}"
        assert (method, path) in registered, f"清单端点未注册: {method} {path}"
