"""Business-specific wellness agents and helpers（惰性加载）。"""

_lazy_map = {
    "WellnessPlanningAgent": ".agent:WellnessPlanningAgent",
    "WellnessProfile": ".schemas:WellnessProfile",
    "WellnessAgentService": ".service:WellnessAgentService",
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
    "WellnessPlanningAgent",
    "WellnessProfile",
    "WellnessAgentService",
]
