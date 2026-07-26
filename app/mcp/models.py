"""MCP 系统 - 数据模型

参考 Yuxi 的 mcp_servers 表（models_business.py:520-621）简化：
- 存储用 JSON 文件（与 Skills 一致，暂不引入数据库）
- enabled 用 bool（Yuxi 用 Integer 1/0，易错）
- to_client_config 按 transport 投影出 MultiServerMCPClient 的连接配置
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class MCPServer(BaseModel):
    """一个 MCP server 的注册信息"""
    slug: str = Field(..., description="唯一标识")
    name: str = Field(default="", description="显示名称")
    description: str = Field(default="", description="描述")
    transport: Literal["stdio", "sse", "streamable_http"] = Field(..., description="传输方式")

    # http 类 transport
    url: Optional[str] = Field(default=None, description="server 地址（sse/streamable_http）")
    headers: dict[str, str] = Field(default_factory=dict, description="请求头（sse/streamable_http）")
    timeout: Optional[int] = Field(default=None, description="HTTP 超时秒数")
    sse_read_timeout: Optional[int] = Field(default=None, description="SSE 读超时秒数")

    # stdio transport
    command: Optional[str] = Field(default=None, description="启动命令（stdio）")
    args: list[str] = Field(default_factory=list, description="命令参数（stdio）")
    env: dict[str, str] = Field(default_factory=dict, description="环境变量（stdio）")

    enabled: bool = True
    disabled_tools: list[str] = Field(default_factory=list, description="禁用的工具名")

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not SLUG_PATTERN.match(v):
            raise ValueError("slug 必须是小写字母、数字、连字符组合，如 'chart-mcp'")
        return v

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "MCPServer":
        if self.transport in ("sse", "streamable_http"):
            if not self.url:
                raise ValueError(f"transport={self.transport} 必须提供 url")
            if not self.url.startswith(("http://", "https://")):
                raise ValueError(f"url 必须是 http(s) 地址: {self.url}")
        if self.transport == "stdio" and not self.command:
            raise ValueError("transport=stdio 必须提供 command")
        return self

    def to_client_config(self) -> dict:
        """投影为 langchain_mcp_adapters MultiServerMCPClient 的连接配置"""
        config: dict = {"transport": self.transport}

        if self.transport == "stdio":
            config["command"] = self.command
            config["args"] = list(self.args)
            if self.env:
                config["env"] = dict(self.env)
        else:
            config["url"] = self.url
            if self.headers:
                config["headers"] = dict(self.headers)
            if self.timeout is not None:
                config["timeout"] = self.timeout
            if self.sse_read_timeout is not None:
                config["sse_read_timeout"] = self.sse_read_timeout

        return config
