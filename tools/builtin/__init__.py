"""内置工具模块（惰性加载）。

仅保证核心工具可直接导入。评测、协议、训练类工具按可用依赖进行可选导入，
避免业务场景仅使用基础能力时被额外模块阻塞。
"""

_lazy_map = {
    "SearchTool": ".search_tool:SearchTool",
    "CalculatorTool": ".calculator:CalculatorTool",
    "MemoryTool": ".memory_tool:MemoryTool",
    "RAGTool": ".rag_tool:RAGTool",
    "NoteTool": ".note_tool:NoteTool",
    "TerminalTool": ".terminal_tool:TerminalTool",
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
    "SearchTool",
    "CalculatorTool",
    "MemoryTool",
    "RAGTool",
    "NoteTool",
    "TerminalTool",
]
