"""记忆类型层模块（惰性加载）。

按照第8章架构设计的记忆类型层：
- WorkingMemory: 工作记忆 - 短期上下文管理
- EpisodicMemory: 情景记忆 - 具体交互事件存储
- SemanticMemory: 语义记忆 - 抽象知识和概念存储
- PerceptualMemory: 感知记忆 - 多模态数据存储
"""

_lazy_map = {
    "WorkingMemory": ".working:WorkingMemory",
    "EpisodicMemory": ".episodic:EpisodicMemory",
    "Episode": ".episodic:Episode",
    "SemanticMemory": ".semantic:SemanticMemory",
    "Entity": ".semantic:Entity",
    "Relation": ".semantic:Relation",
    "PerceptualMemory": ".perceptual:PerceptualMemory",
    "Perception": ".perceptual:Perception",
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
    # 记忆类型
    "WorkingMemory",
    "EpisodicMemory",
    "SemanticMemory",
    "PerceptualMemory",
    # 辅助类
    "Episode",
    "Entity",
    "Relation",
    "Perception",
]
