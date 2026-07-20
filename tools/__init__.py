"""工具系统（惰性加载）。"""

from .base import Tool, ToolParameter
from .registry import ToolRegistry, global_registry
from .response import ToolResponse, ToolStatus, ToolErrorCode
from .circuit_breaker import CircuitBreaker

# 惰性加载内置工具与高级功能，避免拉起完整依赖链。
_lazy_map = {
    "SearchTool": ".builtin.search_tool:SearchTool",
    "CalculatorTool": ".builtin.calculator:CalculatorTool",
    "MemoryTool": ".builtin.memory_tool:MemoryTool",
    "RAGTool": ".builtin.rag_tool:RAGTool",
    "NoteTool": ".builtin.note_tool:NoteTool",
    "TerminalTool": ".builtin.terminal_tool:TerminalTool",
    "ToolChain": ".chain:ToolChain",
    "ToolChainManager": ".chain:ToolChainManager",
    "create_research_chain": ".chain:create_research_chain",
    "create_simple_chain": ".chain:create_simple_chain",
}


def __getattr__(name):
    if name in _lazy_map:
        module_path, attr = _lazy_map[name].rsplit(":", 1)
        import importlib
        mod = importlib.import_module(module_path, __package__)
        obj = getattr(mod, attr)
        globals()[name] = obj
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # 基础工具系统
    "Tool",
    "ToolParameter",
    "ToolRegistry",
    "global_registry",
    "ToolResponse",
    "ToolStatus",
    "ToolErrorCode",
    "CircuitBreaker",
    # 内置工具（惰性）
    "SearchTool",
    "CalculatorTool",
    "MemoryTool",
    "RAGTool",
    "NoteTool",
    "TerminalTool",
    # 工具链功能（惰性）
    "ToolChain",
    "ToolChainManager",
    "create_research_chain",
    "create_simple_chain",
]
