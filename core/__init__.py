"""核心框架模块（惰性加载）。"""

_SUBMODULES = {
    "Agent": ".agent:Agent",
    "HelloAgentsLLM": ".llm:HelloAgentsLLM",
    "Message": ".message:Message",
    "Config": ".config:Config",
    "HelloAgentsException": ".exceptions:HelloAgentsException",
}


def __getattr__(name):
    if name in _SUBMODULES:
        module_path, attr = _SUBMODULES[name].rsplit(":", 1)
        import importlib
        mod = importlib.import_module(module_path, __package__)
        obj = getattr(mod, attr)
        globals()[name] = obj
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Agent",
    "HelloAgentsLLM", 
    "Message",
    "Config",
    "HelloAgentsException",
]