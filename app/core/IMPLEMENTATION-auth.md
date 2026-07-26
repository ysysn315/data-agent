# API Key 鉴权 + 工作空间隔离实现说明（app/core/auth，多租户-lite）

F 轮新增：给此前"占位鉴权"（`get_current_user` 恒返回 dev_user）补上真实的
**API Key 鉴权**与**工作空间隔离**。对标 Yuxi 的完整用户体系（OIDC + JWT + 部门树 +
密码登录 + APIKey 多把），刻意做成演示级 lite 版：**一把 Key 一个用户、一个用户一个工作空间、
不做密码/OIDC/部门树**，取舍见 §4。零新依赖——哈希/常量时间比较/随机数全用标准库
（hashlib / hmac / secrets），不引 passlib、不引 pyjwt。

**兼容铁律**：`settings.auth_enabled` 默认 `False`（demo 模式），此时行为与鉴权落地前
**完全一致**——占位 dev_user、读写全开。鉴权只在 `auth_enabled=True` 时生效。

---

## ① 功能与启用步骤

数据模型两张新表（`app/db/models.py`）：

| 表 | 关键列 | 说明 |
|---|---|---|
| `workspaces` | id / slug（唯一）/ name / created_at | 资源隔离单元（多租户-lite） |
| `users` | id / username（唯一）/ role（admin\|member）/ workspace_id / api_key_hash（sha256）/ api_key_prefix（明文前 8 位）/ enabled / created_at | 一个用户内联一把 Key |

管理 API（`app/api/routes_auth.py`，前缀 `/api/auth`）：

| 端点 | 权限 | 作用 |
|---|---|---|
| `POST /users` | admin | 新建用户并签发 Key，**明文只在本响应返回一次** |
| `GET /users` | admin | 列出用户（不回哈希/明文） |
| `POST /users/{id}/disable` | admin | 禁用用户（其 Key 随即失效） |
| `GET /me` | 登录 | 校验自己的 Key，回显当前身份 |

**启用步骤**：

1. 设 `AUTH_ENABLED=true`（`.env`），重启后端。
2. 首次启动且库中无用户时自动 **bootstrap**：建 `default` 工作空间 + `admin` 用户，
   并把明文 Key 打进 **warning 日志一次**（形如）：
   ```
   [鉴权 bootstrap] 已创建默认工作空间 'default' 与管理员 'admin'。
   新建 admin API Key: da-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   请立即保存：该明文只此一次出现，库中仅存 sha256 哈希，遗失只能重新签发。
   ```
   从启动日志抓这把 Key 即得首个 admin 凭证；bootstrap 幂等，已有用户不再重复建。
3. 用这把 Key 调用受保护接口：`Authorization: Bearer da-...`，或 `POST /api/auth/users`
   给团队签发更多 Key。

```bash
# 从启动日志拿到 admin key 后：
curl -H "Authorization: Bearer da-xxxx" localhost:9900/api/auth/me
# 签发一个 member（返回体里的 api_key 字段是唯一一次明文）
curl -X POST localhost:9900/api/auth/users -H "Authorization: Bearer da-xxxx" \
  -H 'Content-Type: application/json' -d '{"username":"alice","role":"member","workspace":"team-a"}'
```

---

## ② 实现原理

**只存哈希**：`create_user` 用 `secrets.token_hex(16)` 生成 `da-` + 32 hex（128bit 熵）的 Key，
库里只落 `sha256(明文)` 的 64 位 hex 与明文前 8 位（`api_key_prefix`，供 UI/日志识别）。
明文仅在「创建响应」与「bootstrap 日志」各出现一次，此后无法从库中还原。

**constant-time 校验**：`verify_api_key` 先算 `sha256(key)` 按哈希索引命中用户，
再用 `hmac.compare_digest(stored_hash, computed_hash)` 常量时间复核；禁用用户直接判无效。
按哈希命中本身已是精确匹配，`compare_digest` 是"消灭任何非常量时间比较路径"的防御性复核
（若日后改成按 prefix 召回再比对，这行就成了真正的时序防护）。

**开关设计（依赖链 + per-request 缓存）**（`app/core/dependencies.py`）：

```
get_current_user_optional  ──(可选，返回 user|None)
        ▲
get_current_user           ──(登录守卫：auth 下 None→401；demo 恒返回 dev_user)
        ▲
get_admin_user             ──(管理员守卫：role!=admin→403)
```

三者链式依赖，复用 FastAPI 的 per-request 依赖缓存：同一请求里 `get_current_user_optional`
只解析一次，因此"守卫 + 取用户信息"叠加在同一路由上也**只查库一次**。
`demo` 下 `get_current_user_optional` 无 header→None、Bearer→占位 dev_user（与从前逐字节一致），
`get_current_user` 恒返回 dev_user（`role=admin`，故 admin 守卫在 demo 下也放行→写口全开）。
返回 dict 只增键（role/workspace_id），既有 `id`/`username` 不减，老消费方 `.get("id")` 不受影响。

**保护清单（单一事实源）**：`app/core/auth.PROTECTED_ENDPOINTS` 用 `(method, path, level)`
集中登记"哪些口受保护、要什么级别"，路由文件只按此挂 `dependencies=[Depends(...)]`：

| 级别 | 端点 |
|---|---|
| login | sql-examples / terminology 写口；skills 增删改、远程安装 |
| admin | skills 启停；MCP 全部写口 + 连接测试 |
| 开放 | 一切 GET 读口（demo 观感） |

`tests/test_auth.py` 既对清单做矩阵化行为断言（逐口 no-key→401 / member 对 admin 口→403 /
登录口放行），又用 OpenAPI schema 校验清单里每个端点都真实注册，防止清单与路由漂移。

**工作空间隔离（lite）**：技能上传/远程安装时把 `workspace_id` 写进 `skills.share_config`
JSON（复用既有列，零迁移；`share_config` 本就是"可见范围"语义的载体）。`list_skills` 在
`auth_enabled=True` 且非 admin 时过滤为「本 workspace + 内置」；admin 全见；demo 完全不过滤。

---

## ③ Yuxi / SQLBot 怎么做的

- **分层守卫**：`yuxi-reference/backend/server/utils/auth_middleware.py` 有
  `get_current_user`（可选）→ `get_required_user`（401 + 校验绑定部门）→ `get_admin_user`（403）
  → `get_superadmin_user` 四层。本项目收敛为三层（去掉 superadmin），`get_admin_user` 的
  role 校验思路与之一致。
- **API Key 存储**：`yuxi-reference/backend/package/yuxi/storage/postgres/models_business.py:728`
  的 `APIKey` 表用 `key_hash`（sha256, unique index）+ `key_prefix`，`_verify_api_key` 同样
  `sha256(key)` 后按哈希查表、校验 `is_enabled`/`expires_at`。本项目照搬"只存哈希 + prefix 识别"，
  但把 APIKey 内联进 `users`（省一张表与一次 join），砍掉 expires_at/last_used_at。
- **User 模型 + OIDC**：Yuxi 的 `User`（同文件 :52）含 `password_hash`/`department_id`/软删除/
  登录锁定，并支持 JWT + OIDC 双认证路径（`token.startswith("yxkey_")` 分流）。本项目 lite 版
  **不做密码登录、不做 OIDC、不做部门树**，只留 API Key 一条路径。
- **工作空间隔离**：`sqlbot-reference/backend/alembic/versions/020_workspace_ddl.py` 的
  `sys_workspace(id/name/create_time)` + 默认工作空间 id=1，各业务表挂 `oid`（workspace id）做
  资源隔离。本项目 `workspaces` 表对齐其最小形态，隔离粒度落在 skills 上（`share_config.workspace_id`）。
- **share_config**：`yuxi-reference/backend/package/yuxi/utils/share_config.py` 用
  `access_level ∈ {global, department, user}` 描述共享范围。本项目复用 `share_config` 这一 JSON 载体，
  但只放 `workspace_id`（不做 department/user 多级）。

---

## ④ 区别与取舍

- **为什么 API Key 而非 JWT/OIDC**：本项目的调用方是 Agent / 脚本 / CI，不是浏览器会话——
  没有登录页、没有刷新令牌、没有第三方 IdP。一把长效随机 Key 直接鉴权最贴合用法，且无状态、
  零新依赖。JWT 解决的是"无状态分发短时令牌"，OIDC 解决的是"联合登录"，两者的复杂度在这里都用不上；
  真要多租户 SSO 再上 OIDC，接口分层（get_current_user/admin）已为其预留。
- **为什么不引 passlib**：passlib 是给**用户口令**做慢哈希（bcrypt/argon2，故意加盐加轮次抗离线爆破）。
  我们的 Key 是 128bit 高熵随机串，不存在字典/爆破面，sha256 足够且更快——引 passlib 是错配。
- **为什么默认关**：`auth_enabled=False` 保证 demo / 简历演示 / 现有 190+ 测试**零改动**跑通，
  开发者本地不必先建用户才能点接口。鉴权是"生产开关"而非"默认负担"，通过依赖链设计让开/关只切一处。
- **为什么读开放、写保护**：读接口（技能列表、术语、MCP 列表）是 demo 观感的主体，开放它们让
  未登录也能浏览；写接口才改状态、才需归属与审计，故只在写口设闸。这也把鉴权的攻击面收敛到少数几个口。
- **为什么 MCP 全写口 + 连接测试要 admin**：`transport=stdio` 的 `command/args` 等价于在服务器上
  执行任意命令，MCP 配置面本质是命令执行面（见 `app/mcp/IMPLEMENTATION.md` §4）。Yuxi 同样无
  SSRF/命令白名单、靠 admin-only 兜底——这条边界代码层不自作主张放开，故 MCP 写口与 `/test`
  （会真正拉起命令/连接）一律升到 admin；技能启停决定"哪些技能对模型可见"，同属高权，也归 admin。
- **lite 的边界（留给后续轮次）**：一个用户一把 Key（无轮换/过期）、一个用户一个工作空间（无跨空间共享、
  无部门树）、workspace 隔离只落在 skills（数据源行列级权限是 G 轮 `feat/row-col-permission` 的事）。
  这些都是"接口已分层、扩展只加列/加表"的增量，不是重构。
