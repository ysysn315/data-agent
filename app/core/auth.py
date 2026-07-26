"""API Key 鉴权服务（F 轮）

设计取舍详见 app/core/IMPLEMENTATION-auth.md。要点：

- **API Key 而非 JWT/OIDC**：无登录态、无会话、无第三方 IdP，一把随机 Key 直接鉴权，
  最贴合"给 Agent / 脚本 / CI 发一把 Key 调 API"的用法，且零新依赖（hashlib/hmac/secrets 都在标准库）。
- **只存哈希**：库里存 sha256(明文)，明文只在「创建响应」和「bootstrap 日志」里各出现一次
  （对齐 Yuxi APIKey.key_hash，见 yuxi-reference/.../auth_middleware.py:_verify_api_key）。
- **constant-time 校验**：命中哈希后再用 hmac.compare_digest 复核，避免任何非常量时间比较路径。

不引 passlib：passlib 是给「用户口令」做慢哈希（bcrypt/argon2，故意加盐加轮次抗爆破）用的；
我们的 Key 是 128bit 高熵随机串，不存在字典/爆破面，sha256 足够且更快，引 passlib 是错配。
不引 pyjwt：无 JWT 就不需要。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

from loguru import logger

from app.core.settings import settings

# API Key 形如 da- + 32 hex（128bit 熵）。前缀便于人眼/日志识别是本系统的 Key。
API_KEY_PREFIX = "da-"
_API_KEY_NBYTES = 16          # 16 bytes -> 32 hex chars
_API_KEY_PREFIX_LEN = 8       # 明文前 8 位入库便于识别（da- + 5 hex）

# 鉴权成功后对外暴露的用户字段（永不含 api_key_hash）
_PUBLIC_USER_FIELDS = ("id", "username", "role", "workspace_id", "api_key_prefix", "enabled")

# bootstrap 默认工作空间 / 管理员
DEFAULT_WORKSPACE_SLUG = "default"
DEFAULT_ADMIN_USERNAME = "admin"

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
_VALID_ROLES = {ROLE_ADMIN, ROLE_MEMBER}


# ========== 受保护端点清单（单一事实源） ==========
#
# "哪些口受保护、要什么级别" 全部集中在这里；路由文件只是按此挂 Depends，
# tests/test_auth.py 会遍历 app.routes 逐口断言与本清单一致（防止漏挂/挂错）。
# level: "login" = 需登录（admin 或 member 皆可）；"admin" = 仅 admin。
# 读接口一律不入清单 —— 保持开放（demo 观感）。
PROTECTED_ENDPOINTS: list[tuple[str, str, str]] = [
    # SQL 示例库 / 术语库：运营写口，需登录
    ("POST", "/api/sql-examples", "login"),
    ("DELETE", "/api/sql-examples/{example_id}", "login"),
    ("POST", "/api/terminology", "login"),
    ("DELETE", "/api/terminology/{term}", "login"),
    # 技能：增删改 / 远程安装需登录；启停触及"哪些技能对模型可见"，需 admin
    ("POST", "/api/skills", "login"),
    ("PUT", "/api/skills/{slug}", "login"),
    ("DELETE", "/api/skills/{slug}", "login"),
    ("POST", "/api/skills/{slug}/enable", "admin"),
    ("POST", "/api/skills/{slug}/disable", "admin"),
    ("POST", "/api/skills/remote/install", "login"),
    ("POST", "/api/skills/remote/install-batch", "login"),
    # MCP：stdio = 服务器命令执行面，全部写口 + 连接测试一律 admin
    # （对齐 app/mcp/IMPLEMENTATION.md "靠 admin-only 兜底" 的承诺）
    ("POST", "/api/mcp/servers", "admin"),
    ("PUT", "/api/mcp/servers/{slug}", "admin"),
    ("DELETE", "/api/mcp/servers/{slug}", "admin"),
    ("POST", "/api/mcp/servers/{slug}/enable", "admin"),
    ("POST", "/api/mcp/servers/{slug}/disable", "admin"),
    ("POST", "/api/mcp/servers/{slug}/test", "admin"),
]


# ========== Key 生成 / 哈希 ==========

def generate_api_key() -> tuple[str, str, str]:
    """生成一把新 Key，返回 (明文, sha256_hex, 明文前 8 位)。明文只此一次可见。"""
    plaintext = API_KEY_PREFIX + secrets.token_hex(_API_KEY_NBYTES)
    return plaintext, hash_api_key(plaintext), plaintext[:_API_KEY_PREFIX_LEN]


def hash_api_key(key: str) -> str:
    """sha256(明文) 的 64 位 hex（与 Yuxi APIKey.key_hash 口径一致）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def mask_api_key(key: str) -> str:
    """日志/报告打码：只留前缀，尾部星号（避免明文二次落地）。"""
    if len(key) <= _API_KEY_PREFIX_LEN:
        return key
    return f"{key[:_API_KEY_PREFIX_LEN]}...{'*' * 6}"


# ========== 内部：仓储装配 ==========

def _repos():
    """惰性装配 (UserRepository, WorkspaceRepository)（全局 async engine，主循环 await）。"""
    from app.db import get_sessionmaker
    from app.db.repositories import UserRepository, WorkspaceRepository

    sm = get_sessionmaker()
    return UserRepository(sm), WorkspaceRepository(sm)


def _public(user: dict) -> dict:
    """裁剪成对外安全 dict（去掉可能携带的 api_key_hash）。"""
    return {k: user.get(k) for k in _PUBLIC_USER_FIELDS}


# ========== 建用户 ==========

async def create_user(
    username: str,
    role: str = ROLE_MEMBER,
    workspace: str = DEFAULT_WORKSPACE_SLUG,
) -> dict:
    """创建用户并签发一把 API Key。

    返回安全 dict + **仅此一次**的明文 api_key 字段：{id, username, role, workspace_id,
    api_key_prefix, enabled, api_key(明文)}。明文不入库（库里只有 sha256）。
    """
    from app.db import ensure_initialized

    username = (username or "").strip()
    if not username:
        raise ValueError("username 不能为空")
    if role not in _VALID_ROLES:
        raise ValueError(f"role 只能是 {sorted(_VALID_ROLES)}，当前: {role}")

    ensure_initialized()
    user_repo, ws_repo = _repos()

    ws = await ws_repo.get_or_create((workspace or DEFAULT_WORKSPACE_SLUG).strip() or DEFAULT_WORKSPACE_SLUG)

    plaintext, key_hash, key_prefix = generate_api_key()
    created = await user_repo.create(
        username=username,
        role=role,
        workspace_id=ws["id"],
        api_key_hash=key_hash,
        api_key_prefix=key_prefix,
    )
    created["api_key"] = plaintext  # 明文仅在此返回一次
    return created


async def list_users() -> list[dict]:
    """列出全部用户（安全 dict，不含哈希）。供 admin 管理 API。"""
    from app.db import ensure_initialized

    ensure_initialized()
    user_repo, _ = _repos()
    return await user_repo.list_all()


async def disable_user(user_id: int) -> Optional[dict]:
    """禁用用户（其 Key 随即失效）。不存在返回 None。"""
    from app.db import ensure_initialized

    ensure_initialized()
    user_repo, _ = _repos()
    return await user_repo.set_enabled(user_id, False)


# ========== 校验 ==========

async def verify_api_key(key: str) -> Optional[dict]:
    """校验一把明文 Key，返回安全用户 dict（不含哈希）或 None。

    调用方（get_current_user*）保证表已建（startup / 测试 fixture 已 ensure_initialized）。
    先按 sha256 命中，再 hmac.compare_digest 常量时间复核；禁用用户视为无效。
    """
    if not key:
        return None

    key_hash = hash_api_key(key)
    user_repo, _ = _repos()
    user = await user_repo.get_by_api_key_hash(key_hash)
    if user is None:
        return None
    if not user.get("enabled", False):
        return None
    # 命中即相等，这里的 compare_digest 是"消灭任何非常量时间比较"的防御性复核
    if not hmac.compare_digest(user.get("api_key_hash", ""), key_hash):
        return None
    return _public(user)


# ========== bootstrap（首启无用户时自建 default 工作空间 + admin） ==========

async def bootstrap() -> Optional[str]:
    """auth_enabled 且库中无用户时，自建 default 工作空间 + admin 用户。

    幂等：已有用户则直接返回 None，不重复创建。返回新 admin 的明文 Key（仅本次），
    并 logger.warning 醒目打印一次（形如：新建 admin API Key: da-xxxx...，请立即保存）。
    """
    if not settings.auth_enabled:
        return None

    from app.db import ensure_initialized

    ensure_initialized()
    user_repo, _ = _repos()
    if await user_repo.count() > 0:
        return None  # 幂等：已初始化过

    created = await create_user(
        username=DEFAULT_ADMIN_USERNAME,
        role=ROLE_ADMIN,
        workspace=DEFAULT_WORKSPACE_SLUG,
    )
    plaintext = created["api_key"]
    logger.warning(
        "=" * 60 + "\n"
        f"[鉴权 bootstrap] 已创建默认工作空间 '{DEFAULT_WORKSPACE_SLUG}' 与管理员 "
        f"'{DEFAULT_ADMIN_USERNAME}'。\n"
        f"新建 admin API Key: {plaintext}\n"
        "请立即保存：该明文只此一次出现，库中仅存 sha256 哈希，遗失只能重新签发。\n"
        + "=" * 60
    )
    return plaintext
