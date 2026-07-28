"""MCPService 测试：注册表 CRUD / 配置投影 / 缓存失效 / 真实 stdio server 加载"""

import pytest

from app.mcp.models import MCPServer
from app.mcp.service import MCPService


def make_stdio_server(slug: str = "test-mcp", **kwargs) -> MCPServer:
    return MCPServer(
        slug=slug,
        name="测试 server",
        transport="stdio",
        command="python",
        args=["-c", "pass"],
        **kwargs,
    )


def test_transport_validation():
    with pytest.raises(ValueError, match="url"):
        MCPServer(slug="a-b", transport="sse")
    with pytest.raises(ValueError, match="command"):
        MCPServer(slug="a-b", transport="stdio")
    with pytest.raises(ValueError, match="http"):
        MCPServer(slug="a-b", transport="sse", url="ftp://x")
    with pytest.raises(ValueError, match="slug"):
        MCPServer(slug="Bad_Slug", transport="stdio", command="x")


def test_to_client_config_gates_fields_by_transport():
    stdio = make_stdio_server(env={"K": "V"}, headers={"H": "1"})
    config = stdio.to_client_config()
    assert config["command"] == "python"
    assert config["env"] == {"K": "V"}
    assert "headers" not in config  # stdio 不带 headers

    http = MCPServer(
        slug="chart",
        transport="streamable_http",
        url="http://localhost:1122/mcp",
        headers={"A": "b"},
        timeout=5,
        command="should-be-ignored",
    )
    config = http.to_client_config()
    assert config["url"] == "http://localhost:1122/mcp"
    assert config["headers"] == {"A": "b"}
    assert "command" not in config  # http 不带 command


def test_registry_crud_and_persistence(tmp_path):
    config_path = tmp_path / "mcp_servers.json"
    service = MCPService(config_path=config_path)

    service.create_server(make_stdio_server())
    assert service.get_server("test-mcp") is not None
    with pytest.raises(ValueError, match="已存在"):
        service.create_server(make_stdio_server())

    # 持久化：新实例能读回
    service2 = MCPService(config_path=config_path)
    assert service2.get_server("test-mcp").command == "python"

    service2.set_enabled("test-mcp", False)
    assert not MCPService(config_path=config_path).get_server("test-mcp").enabled

    assert service2.delete_server("test-mcp")
    assert MCPService(config_path=config_path).get_server("test-mcp") is None


async def test_load_tools_skips_disabled_and_missing(tmp_path):
    service = MCPService(config_path=tmp_path / "mcp.json")
    service.create_server(make_stdio_server(enabled=False))
    # 未启用 + 不存在的 server：都返回空，不抛异常
    tools = await service.load_tools(["test-mcp", "ghost"])
    assert tools == []


async def test_load_tools_from_real_stdio_server(tmp_path):
    """端到端：起一个真实 FastMCP stdio server，通过注册表加载其工具"""
    server_script = tmp_path / "math_server.py"
    server_script.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('math')\n"
        "@mcp.tool()\n"
        "def add(a: int, b: int) -> int:\n"
        "    '''两数相加'''\n"
        "    return a + b\n"
        "mcp.run(transport='stdio')\n",
        encoding="utf-8",
    )

    import sys

    service = MCPService(config_path=tmp_path / "mcp.json", load_timeout=30)
    service.create_server(
        MCPServer(
            slug="math",
            transport="stdio",
            command=sys.executable,
            args=[str(server_script)],
        )
    )

    tools = await service.load_tools(["math"])
    assert [t.name for t in tools] == ["add"]

    result = await tools[0].ainvoke({"a": 2, "b": 3})
    assert "5" in str(result)

    # disabled_tools 过滤只影响返回
    server = service.get_server("math")
    server.disabled_tools = ["add"]
    assert await service.load_tools(["math"]) == []
