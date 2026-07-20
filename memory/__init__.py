"""HelloAgents记忆系统模块（惰性加载）。

按照第8章架构设计的分层记忆系统：
- Memory Core Layer: 记忆核心层
- Memory Types Layer: 记忆类型层
- Storage Layer: 存储层
- Integration Layer: 集成层
"""

_lazy_map = {
    "MemoryManager": ".manager:MemoryManager",
    "WorkingMemory": ".types.working:WorkingMemory",
    "EpisodicMemory": ".types.episodic:EpisodicMemory",
    "SemanticMemory": ".types.semantic:SemanticMemory",
    "PerceptualMemory": ".types.perceptual:PerceptualMemory",
    "DocumentStore": ".storage.document_store:DocumentStore",
    "SQLiteDocumentStore": ".storage.document_store:SQLiteDocumentStore",
    "MemoryItem": ".base:MemoryItem",
    "MemoryConfig": ".base:MemoryConfig",
    "BaseMemory": ".base:BaseMemory",
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
    # Core Layer
    "MemoryManager",
    # Memory Types
    "WorkingMemory",
    "EpisodicMemory",
    "SemanticMemory",
    "PerceptualMemory",
    # Storage Layer
    "DocumentStore",
    "SQLiteDocumentStore",
    # Base
    "MemoryItem",
    "MemoryConfig",
    "BaseMemory",
]
