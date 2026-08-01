"""MCP 系统 - 业务逻辑层

参考 Yuxi agents/mcp/service.py，修正其已知问题：
- 工具加载套 asyncio.wait_for（Yuxi 的 stdio 无超时，子进程挂起会卡死请求）
- test_server 不要求 enabled（Yuxi 无法在启用前验证 server）
- 缓存键 = slug + 配置哈希，配置变更自动失效；CRUD 时显式失效

注册表持久化：save_dir/mcp_servers.json（原子写，与 Skills 同思路暂不引入数据库）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Optional

from loguru import logger

from app.mcp.models import MCPServer

DEFAULT_LOAD_TIMEOUT_SECONDS = 20.0


class MCPService:
    """MCP server 注册表 + 工具加载"""

    def __init__(
        self,
        config_path: Optional[str | Path] = None,
        load_timeout: float = DEFAULT_LOAD_TIMEOUT_SECONDS,
        repo=None,
        runner=None,
    ):
        """
        Args:
            config_path: 注册表 JSON 文件路径（None=纯内存，测试用）
            load_timeout: 单个 server 工具加载超时秒数
            repo: 数据库存储后端（app.db.repositories.MCPRepository）。
                  传入即启用「DB 版」：注册表从数据库读写，config_path 仅作历史 JSON 迁移源。
            runner: sync→async 桥（app.db.run_sync），DB 版必传。
        """
        self.config_path = Path(config_path) if config_path else None
        self.load_timeout = load_timeout
        self._repo = repo
        self._run = runner
        self._servers: dict[str, MCPServer] = {}
        # 工具缓存：f"{slug}:{config_hash}" -> list[BaseTool]（存未过滤的全量）
        self._tools_cache: dict[str, list] = {}
        self._load_registry()

    # ========== 注册表持久化 ==========

    def _load_registry(self) -> None:
        if self._repo is not None:
            self._load_registry_db()
            return
        if self.config_path is None or not self.config_path.exists():
            return
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            for item in data.get("servers", []):
                server = MCPServer(**item)
                self._servers[server.slug] = server
            logger.info(f"加载 MCP 注册表: {len(self._servers)} 个 server")
        except Exception as e:
            # 配置文件损坏应显式暴露而不是静默清空
            raise ValueError(f"MCP 注册表解析失败 {self.config_path}: {e}")

    def _load_registry_db(self) -> None:
        """DB 版加载：表空且有历史 JSON 时一次性迁移入库（之后 DB 为唯一真源）。"""
        servers = self._run(self._repo.list_all())
        if not servers and self.config_path is not None and self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception as e:
                raise ValueError(f"MCP 注册表解析失败 {self.config_path}: {e}")
            legacy = [MCPServer(**item) for item in data.get("servers", [])]
            if legacy:
                self._run(self._repo.replace_all(legacy))
                logger.info(f"MCP 注册表 JSON→DB 一次性迁移: {len(legacy)} 个 server")
                servers = legacy
        for server in servers:
            self._servers[server.slug] = server
        if servers:
            logger.info(f"加载 MCP 注册表(DB): {len(servers)} 个 server")

    def _save_registry(self) -> None:
        if self._repo is not None:
            # 整表替换，对应 JSON 版的整文件原子重写（注册表规模小，代价可忽略）
            self._run(self._repo.replace_all(list(self._servers.values())))
            return
        if self.config_path is None:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"servers": [s.model_dump() for s in self._servers.values()]}
        # 原子写：tmp + rename
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(self.config_path)

    # ========== CRUD ==========

    def list_servers(self) -> list[MCPServer]:
        return list(self._servers.values())

    def get_server(self, slug: str) -> Optional[MCPServer]:
        return self._servers.get(slug)

    def create_server(self, server: MCPServer) -> MCPServer:
        if server.slug in self._servers:
            raise ValueError(f"MCP server 已存在: {server.slug}")
        self._servers[server.slug] = server
        self._save_registry()
        return server

    def update_server(self, slug: str, server: MCPServer) -> MCPServer:
        if slug not in self._servers:
            raise ValueError(f"MCP server 不存在: {slug}")
        if server.slug != slug:
            raise ValueError(f"slug 不可修改: {slug} -> {server.slug}")
        self._servers[slug] = server
        self._invalidate(slug)
        self._save_registry()
        return server

    def delete_server(self, slug: str) -> bool:
        if slug not in self._servers:
            return False
        del self._servers[slug]
        self._invalidate(slug)
        self._save_registry()
        return True

    def set_enabled(self, slug: str, enabled: bool) -> MCPServer:
        server = self._servers.get(slug)
        if not server:
            raise ValueError(f"MCP server 不存在: {slug}")
        server.enabled = enabled
        self._invalidate(slug)
        self._save_registry()
        return server

    # ========== 工具加载 ==========

    @staticmethod
    def _config_hash(config: dict) -> str:
        raw = json.dumps(config, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _invalidate(self, slug: str) -> None:
        stale = [k for k in self._tools_cache if k.startswith(f"{slug}:")]
        for key in stale:
            del self._tools_cache[key]

    async def _fetch_tools(self, slug: str, config: dict) -> list:
        """连接 server 拉取工具列表（带超时）"""
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient({slug: config})
        return await asyncio.wait_for(client.get_tools(), timeout=self.load_timeout)

    async def _load_one(self, slug: str) -> list:
        """加载单个已启用 server 的工具（缓存 + 失败隔离，返回 [] 不抛出）"""
        server = self._servers.get(slug)
        if not server:
            logger.warning(f"MCP server 不存在: {slug}")
            return []
        if not server.enabled:
            logger.info(f"MCP server 未启用，跳过: {slug}")
            return []

        config = server.to_client_config()
        cache_key = f"{slug}:{self._config_hash(config)}"

        if cache_key in self._tools_cache:
            tools = self._tools_cache[cache_key]
        else:
            try:
                tools = await self._fetch_tools(slug, config)
            except asyncio.TimeoutError:
                logger.error(f"MCP server 工具加载超时（{self.load_timeout}s）: {slug}")
                return []
            except Exception as e:
                logger.error(f"MCP server 工具加载失败 {slug}: {e}")
                return []
            self._tools_cache[cache_key] = tools
            logger.info(f"MCP server {slug} 加载了 {len(tools)} 个工具")

        # disabled_tools 只影响返回，不影响缓存
        if server.disabled_tools:
            disabled = set(server.disabled_tools)
            tools = [t for t in tools if t.name not in disabled]
        return tools

    async def load_tools(self, slugs: list[str]) -> list:
        """并行加载多个 server 的工具（单点失败不影响其他）"""
        unique = list(dict.fromkeys(slugs))
        results = await asyncio.gather(*(self._load_one(s) for s in unique))
        tools = []
        seen_names: set[str] = set()
        for batch in results:
            for tool in batch:
                if tool.name not in seen_names:
                    seen_names.add(tool.name)
                    tools.append(tool)
        return tools

    async def test_server(self, slug: str) -> dict:
        """测试连接（不要求 enabled、不写缓存），返回工具名列表"""
        server = self._servers.get(slug)
        if not server:
            raise ValueError(f"MCP server 不存在: {slug}")

        tools = await self._fetch_tools(slug, server.to_client_config())
        return {
            "slug": slug,
            "tool_count": len(tools),
            "tools": [{"name": t.name, "description": (t.description or "")[:200]} for t in tools],
        }
