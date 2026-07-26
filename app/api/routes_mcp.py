"""MCP 系统 - API 路由

安全边界：transport=stdio 的 command/args 等价于在服务器上执行任意命令，MCP 配置面
本质是命令执行面。因此 auth_enabled=True 时**全部写口 + 连接测试一律需 admin**
（get_admin_user），对齐 app/mcp/IMPLEMENTATION.md "靠 admin-only 兜底" 的承诺。
读口（list/get）保持开放。demo（auth_enabled=False）下守卫恒放行，行为与从前一致。
受保护清单集中在 app/core/auth.PROTECTED_ENDPOINTS。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.dependencies import get_admin_user, get_mcp_service
from app.mcp.models import MCPServer
from app.mcp.service import MCPService

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/servers", response_model=list[MCPServer])
async def list_servers(mcp_service: MCPService = Depends(get_mcp_service)):
    """列出所有 MCP server"""
    return mcp_service.list_servers()


@router.get("/servers/{slug}", response_model=MCPServer)
async def get_server(slug: str, mcp_service: MCPService = Depends(get_mcp_service)):
    """获取单个 MCP server 配置"""
    server = mcp_service.get_server(slug)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"MCP server 不存在: {slug}")
    return server


@router.post(
    "/servers",
    response_model=MCPServer,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_admin_user)],
)
async def create_server(server: MCPServer, mcp_service: MCPService = Depends(get_mcp_service)):
    """注册 MCP server"""
    try:
        return mcp_service.create_server(server)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.put("/servers/{slug}", response_model=MCPServer, dependencies=[Depends(get_admin_user)])
async def update_server(
    slug: str, server: MCPServer, mcp_service: MCPService = Depends(get_mcp_service)
):
    """更新 MCP server 配置"""
    try:
        return mcp_service.update_server(slug, server)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete(
    "/servers/{slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_admin_user)],
)
async def delete_server(slug: str, mcp_service: MCPService = Depends(get_mcp_service)):
    """删除 MCP server"""
    if not mcp_service.delete_server(slug):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"MCP server 不存在: {slug}")


@router.post("/servers/{slug}/enable", response_model=MCPServer, dependencies=[Depends(get_admin_user)])
async def enable_server(slug: str, mcp_service: MCPService = Depends(get_mcp_service)):
    """启用 MCP server"""
    try:
        return mcp_service.set_enabled(slug, True)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/servers/{slug}/disable", response_model=MCPServer, dependencies=[Depends(get_admin_user)])
async def disable_server(slug: str, mcp_service: MCPService = Depends(get_mcp_service)):
    """禁用 MCP server"""
    try:
        return mcp_service.set_enabled(slug, False)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/servers/{slug}/test", dependencies=[Depends(get_admin_user)])
async def test_server(slug: str, mcp_service: MCPService = Depends(get_mcp_service)):
    """测试连接：拉取工具列表（允许测试未启用的 server）

    连接测试会真正拉起 stdio 命令/发起 http 连接，属命令执行面，故与写口同级需 admin。
    """
    try:
        return await mcp_service.test_server(slug)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"连接失败: {e}"
        )
