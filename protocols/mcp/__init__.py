"""MCP (Model Context Protocol) 协议实现

基于 fastmcp 和 mcp 库的封装，提供简洁的 API 用于：
- 创建 MCP 服务器（fastmcp 为**可选**依赖）
- 连接 MCP 服务器（fastmcp 为**可选**依赖）
- 管理模型上下文（无额外依赖）

Phase 3.0-4：``server`` 与 ``client`` 均已软依赖化，import 本包**永远不会**因缺少
``fastmcp`` 而失败；缺依赖时只有在实例化 ``MCPServer`` / ``MCPClient`` 时才报友好错误。
``MCP_SERVER_AVAILABLE`` / ``MCP_CLIENT_AVAILABLE`` 供调用方提前探测。
"""

from .utils import create_context, parse_context
from .server import (
    FASTMCP_AVAILABLE as MCP_SERVER_AVAILABLE,
    MCPServer,
    MCPServerBuilder,
    create_example_server,
)
from .client import (
    FASTMCP_AVAILABLE as MCP_CLIENT_AVAILABLE,
    MCPClient,
)


__all__ = [
    "MCP_CLIENT_AVAILABLE",
    "MCP_SERVER_AVAILABLE",
    "MCPClient",
    "MCPServer",
    "MCPServerBuilder",
    "create_context",
    "create_example_server",
    "parse_context",
]

