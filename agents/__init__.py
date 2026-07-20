"""Agent实现模块 - HelloAgents原生Agent范式（惰性加载）。"""


def __getattr__(name):
    if name == "ReActAgent":
        from .react_agent import ReActAgent
        globals()["ReActAgent"] = ReActAgent
        return ReActAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ReActAgent",
]
