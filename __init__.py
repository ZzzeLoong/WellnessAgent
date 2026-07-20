"""
HelloAgents - 灵活、可扩展的多智能体框架

基于OpenAI原生API构建，提供简洁高效的智能体开发体验。
"""

# 配置第三方库的日志级别，减少噪音
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("qdrant_client").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("neo4j").setLevel(logging.WARNING)
logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)


def __getattr__(name):
    """惰性加载：仅在属性被访问时触发 import，避免 import 时拉起完整依赖链。"""
    _lazy = {
        "HelloAgentsLLM": ".core.llm:HelloAgentsLLM",
        "Config": ".core.config:Config",
        "Message": ".core.message:Message",
        "HelloAgentsException": ".core.exceptions:HelloAgentsException",
        "ReActAgent": ".agents.react_agent:ReActAgent",
        "ToolRegistry": ".tools.registry:ToolRegistry",
        "global_registry": ".tools.registry:global_registry",
        "SearchTool": ".tools.builtin.search_tool:SearchTool",
        "search": ".tools.builtin.search_tool:search",
        "CalculatorTool": ".tools.builtin.calculator:CalculatorTool",
        "calculate": ".tools.builtin.calculator:calculate",
        "ToolChain": ".tools.chain:ToolChain",
        "ToolChainManager": ".tools.chain:ToolChainManager",
    }
    if name in _lazy:
        module_path, attr = _lazy[name].rsplit(":", 1)
        import importlib
        mod = importlib.import_module(module_path, __package__)
        obj = getattr(mod, attr)
        globals()[name] = obj
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # 核心组件
    "HelloAgentsLLM",
    "Config",
    "Message",
    "HelloAgentsException",
    # Agent范式
    "ReActAgent",
    # 工具系统
    "ToolRegistry",
    "global_registry",
    "SearchTool",
    "search",
    "CalculatorTool",
    "calculate",
    "ToolChain",
    "ToolChainManager",
]

