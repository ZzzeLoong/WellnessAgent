"""存储层模块（惰性加载）。

按照第8章架构设计的存储层：
- DocumentStore: 文档存储
- QdrantVectorStore: Qdrant向量存储
- Neo4jGraphStore: Neo4j图存储
"""

_lazy_map = {
    "QdrantVectorStore": ".qdrant_store:QdrantVectorStore",
    "QdrantConnectionManager": ".qdrant_store:QdrantConnectionManager",
    "Neo4jGraphStore": ".neo4j_store:Neo4jGraphStore",
    "DocumentStore": ".document_store:DocumentStore",
    "SQLiteDocumentStore": ".document_store:SQLiteDocumentStore",
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
    "QdrantVectorStore",
    "QdrantConnectionManager",
    "Neo4jGraphStore",
    "DocumentStore",
    "SQLiteDocumentStore",
]
