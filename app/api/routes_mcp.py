"""MCP 系统 - API 路由

注：当前无真实鉴权（get_current_user 为占位实现）。MCP 配置等价于
在服务器上执行任意命令（stdio transport），生产环境必须先落地鉴权。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.dependencies import get_mcp_service
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


@router.post("/servers", response_model=MCPServer, status_code=status.HTTP_201_CREATED)
async def create_server(server: MCPServer, mcp_service: MCPService = Depends(get_mcp_service)):
    """注册 MCP server"""
    try:
        return mcp_service.create_server(server)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.put("/servers/{slug}", response_model=MCPServer)
async def update_server(
    slug: str, server: MCPServer, mcp_service: MCPService = Depends(get_mcp_service)
):
    """更新 MCP server 配置"""
    try:
        return mcp_service.update_server(slug, server)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/servers/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(slug: str, mcp_service: MCPService = Depends(get_mcp_service)):
    """删除 MCP server"""
    if not mcp_service.delete_server(slug):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"MCP server 不存在: {slug}")


@router.post("/servers/{slug}/enable", response_model=MCPServer)
async def enable_server(slug: str, mcp_service: MCPService = Depends(get_mcp_service)):
    """启用 MCP server"""
    try:
        return mcp_service.set_enabled(slug, True)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/servers/{slug}/disable", response_model=MCPServer)
async def disable_server(slug: str, mcp_service: MCPService = Depends(get_mcp_service)):
    """禁用 MCP server"""
    try:
        return mcp_service.set_enabled(slug, False)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/servers/{slug}/test")
async def test_server(slug: str, mcp_service: MCPService = Depends(get_mcp_service)):
    """测试连接：拉取工具列表（允许测试未启用的 server）"""
    try:
        return await mcp_service.test_server(slug)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"连接失败: {e}"
        )
