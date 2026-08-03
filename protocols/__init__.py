"""智能体通信协议模块

本模块可提供三种智能体通信协议：
- MCP (Model Context Protocol): 模型上下文协议
- A2A (Agent-to-Agent Protocol): 智能体间通信协议
- ANP (Agent Network Protocol): 智能体网络协议

导入示例：
    >>> from protocols import MCPClient, MCPServer
    >>> from protocols.mcp import MCPServer

Phase 3.0-4：本仓库只保留了 ``mcp`` 子包（``a2a`` / ``anp`` 未随包引入），
而旧代码在顶层**硬 import** ``.a2a`` / ``.anp``，使得 ``import protocols.mcp.server``
在触发本 ``__init__`` 时先炸在 ``ModuleNotFoundError``。现在三个协议子包一律
按「存在则导出、缺失则给出会在使用时报错的占位符」处理，import 永不失败。
``MCP_AVAILABLE`` / ``A2A_AVAILABLE`` / ``ANP_AVAILABLE`` 供调用方探测。
"""

from .base import Protocol, ProtocolType


def _missing(feature: str, hint: str):
    """构造一个「调用/实例化时才报错」的占位符工厂。"""

    class _Missing:
        def __init__(self, *args, **kwargs):
            raise ImportError(f"{feature} is unavailable: {hint}")

        def __call__(self, *args, **kwargs):
            raise ImportError(f"{feature} is unavailable: {hint}")

    return _Missing


# ---------------------------------------------------------------- MCP（软依赖）
try:
    from .mcp import (
        MCP_CLIENT_AVAILABLE,
        MCP_SERVER_AVAILABLE,
        MCPClient,
        MCPServer,
        create_context,
        parse_context,
    )

    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - mcp 子包缺失
    MCP_AVAILABLE = False
    MCP_CLIENT_AVAILABLE = False
    MCP_SERVER_AVAILABLE = False
    _mcp_hint = "install fastmcp (pip install fastmcp)"
    MCPClient = _missing("MCPClient", _mcp_hint)
    MCPServer = _missing("MCPServer", _mcp_hint)
    create_context = _missing("create_context", _mcp_hint)
    parse_context = _missing("parse_context", _mcp_hint)

# ---------------------------------------------------------------- A2A（可选子包）
try:
    from .a2a import (  # type: ignore[import-not-found]
        A2AAgent,
        A2AServer,
        A2AClient,
        AgentNetwork,
        AgentRegistry,
        A2AMessage,
        MessageType,
        create_message,
        parse_message,
    )

    A2A_AVAILABLE = True
except ImportError:
    A2A_AVAILABLE = False
    _a2a_hint = "the 'protocols.a2a' subpackage is not bundled in this repository"
    A2AAgent = _missing("A2AAgent", _a2a_hint)
    A2AServer = _missing("A2AServer", _a2a_hint)
    A2AClient = _missing("A2AClient", _a2a_hint)
    AgentNetwork = _missing("AgentNetwork", _a2a_hint)
    AgentRegistry = _missing("AgentRegistry", _a2a_hint)
    A2AMessage = _missing("A2AMessage", _a2a_hint)
    MessageType = _missing("MessageType", _a2a_hint)
    create_message = _missing("create_message", _a2a_hint)
    parse_message = _missing("parse_message", _a2a_hint)

# ---------------------------------------------------------------- ANP（可选子包）
try:
    from .anp import (  # type: ignore[import-not-found]
        ANPDiscovery,
        ANPNetwork,
        ServiceInfo,
        register_service,
        discover_service,
    )

    ANP_AVAILABLE = True
except ImportError:
    ANP_AVAILABLE = False
    _anp_hint = "the 'protocols.anp' subpackage is not bundled in this repository"
    ANPDiscovery = _missing("ANPDiscovery", _anp_hint)
    ANPNetwork = _missing("ANPNetwork", _anp_hint)
    ServiceInfo = _missing("ServiceInfo", _anp_hint)
    register_service = _missing("register_service", _anp_hint)
    discover_service = _missing("discover_service", _anp_hint)


__all__ = [
    # 基础协议
    "Protocol",
    "ProtocolType",

    # 可用性探测
    "MCP_AVAILABLE",
    "MCP_CLIENT_AVAILABLE",
    "MCP_SERVER_AVAILABLE",
    "A2A_AVAILABLE",
    "ANP_AVAILABLE",

    # MCP 协议
    "MCPClient",
    "MCPServer",
    "create_context",
    "parse_context",

    # A2A 协议
    "A2AAgent",
    "A2AServer",
    "A2AClient",
    "AgentNetwork",
    "AgentRegistry",
    "A2AMessage",
    "MessageType",
    "create_message",
    "parse_message",

    # ANP 协议
    "ANPDiscovery",
    "ANPNetwork",
    "ServiceInfo",
    "register_service",
    "discover_service",
]

