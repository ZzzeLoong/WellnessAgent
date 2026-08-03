"""memory/types/perceptual.py 单测（Phase 3.0 · P3.0-1 / P3.0-2）。

覆盖两个前置修复：

- **P3.0-2**：``update()`` 曾引用不存在的 ``self.vector_store``（本类只有
  ``self.vector_stores`` 复数 dict），``AttributeError`` 被外层 ``except`` 静默
  吞掉，导致**向量永不更新**。修复后应按模态写入正确的集合。
- **P3.0-1**：CLIP/CLAP 改懒加载 + 默认关闭。构造 ``PerceptualMemory``
  **绝不能**触发 ``from_pretrained``（否则 LRU 50 个用户实例会各加载一遍模型）。

由于本仓库的可选依赖（qdrant-client / transformers / sentence-transformers）
未必安装，测试统一注入替身：嵌入器、SQLite 文档存储、Qdrant 连接管理器、
以及一个记录调用次数的假 ``transformers`` 模块。
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from typing import Any, Dict, List

import pytest

from memory.base import MemoryConfig, MemoryItem
import memory.storage.qdrant_store as qdrant_store_module
import memory.types.perceptual as perceptual_module
from memory.types.perceptual import PerceptualMemory


# ---------------------------------------------------------------- 替身
class FakeEmbedder:
    """确定性文本嵌入替身（固定 8 维，避免依赖真实模型）。"""

    dimension = 8

    def encode(self, text):
        base = float(len(text or "")) or 1.0
        return [base + i for i in range(self.dimension)]


class FakeVectorStore:
    """记录 add_vectors / delete_memories 调用的 Qdrant 替身。"""

    def __init__(self, collection_name: str, vector_size: int):
        self.collection_name = collection_name
        self.vector_size = vector_size
        self.added: List[Dict[str, Any]] = []
        self.deleted: List[List[str]] = []

    def add_vectors(self, vectors, metadata, ids):
        self.added.append({"vectors": vectors, "metadata": metadata, "ids": ids})
        return list(ids)

    def search_similar(self, query_vector, limit=10, where=None):
        return []

    def delete_memories(self, ids):
        self.deleted.append(list(ids))
        return True

    def get_collection_stats(self):
        return {"store_type": "fake", "collection": self.collection_name}


class FakeConnectionManager:
    """替换 QdrantConnectionManager：按集合名返回独立的 FakeVectorStore。"""

    created: Dict[str, FakeVectorStore] = {}

    @classmethod
    def get_instance(cls, url=None, api_key=None, collection_name="c", vector_size=384, distance="cosine", **kwargs):
        store = FakeVectorStore(collection_name, vector_size)
        cls.created[collection_name] = store
        return store


class FakeDocStore:
    """最小 SQLite 文档存储替身。"""

    def __init__(self, db_path: str = ""):
        self.db_path = db_path
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.update_calls: List[Dict[str, Any]] = []

    def add_memory(self, memory_id, user_id, content, memory_type, timestamp, importance, properties=None):
        self.rows[memory_id] = {
            "memory_id": memory_id,
            "user_id": user_id,
            "content": content,
            "memory_type": memory_type,
            "timestamp": timestamp,
            "importance": importance,
            "properties": properties or {},
        }
        return memory_id

    def get_memory(self, memory_id):
        return self.rows.get(memory_id)

    def update_memory(self, memory_id, content=None, importance=None, properties=None):
        self.update_calls.append(
            {"memory_id": memory_id, "content": content, "importance": importance, "properties": properties}
        )
        row = self.rows.get(memory_id)
        if not row:
            return False
        if content is not None:
            row["content"] = content
        if importance is not None:
            row["importance"] = importance
        if properties:
            row["properties"].update(properties)
        return True

    def delete_memory(self, memory_id):
        return self.rows.pop(memory_id, None) is not None

    def search_memories(self, user_id=None, memory_type=None, limit=10, **kwargs):
        return [r for r in self.rows.values() if memory_type is None or r["memory_type"] == memory_type][:limit]

    def get_database_stats(self):
        return {"store_type": "fake", "memories_count": len(self.rows)}


class LoadRecorder:
    """记录 from_pretrained 调用次数的计数器。"""

    def __init__(self):
        self.clip_calls: List[str] = []
        self.clap_calls: List[str] = []

    @property
    def total(self) -> int:
        return len(self.clip_calls) + len(self.clap_calls)


def _install_fake_transformers(monkeypatch, recorder: LoadRecorder, *, fail: bool = False):
    """把一个假的 ``transformers`` 模块塞进 sys.modules 并记录加载调用。"""
    module = types.ModuleType("transformers")

    class _Config:
        projection_dim = 512

    class _FakeCLIPModel:
        config = _Config()

        @classmethod
        def from_pretrained(cls, name, *args, **kwargs):
            recorder.clip_calls.append(name)
            if fail:
                raise RuntimeError("模型下载失败（测试注入）")
            return cls()

    class _FakeCLIPProcessor:
        @classmethod
        def from_pretrained(cls, name, *args, **kwargs):
            return cls()

    class _FakeClapModel:
        config = _Config()

        @classmethod
        def from_pretrained(cls, name, *args, **kwargs):
            recorder.clap_calls.append(name)
            if fail:
                raise RuntimeError("模型下载失败（测试注入）")
            return cls()

    class _FakeClapProcessor:
        @classmethod
        def from_pretrained(cls, name, *args, **kwargs):
            return cls()

    module.CLIPModel = _FakeCLIPModel
    module.CLIPProcessor = _FakeCLIPProcessor
    module.ClapModel = _FakeClapModel
    module.ClapProcessor = _FakeClapProcessor
    monkeypatch.setitem(sys.modules, "transformers", module)
    return module


@pytest.fixture()
def perceptual_env(monkeypatch, tmp_path):
    """提供构造 PerceptualMemory 所需的全部替身。"""
    FakeConnectionManager.created = {}
    monkeypatch.setattr(perceptual_module, "get_text_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(perceptual_module, "get_dimension", lambda default=384: FakeEmbedder.dimension)
    monkeypatch.setattr(perceptual_module, "SQLiteDocumentStore", FakeDocStore)
    monkeypatch.setattr(qdrant_store_module, "QdrantConnectionManager", FakeConnectionManager)
    # 两个开关默认必须是关闭语义：显式删掉以验证「未设置即关闭」。
    monkeypatch.delenv("WELLNESS_CLIP_ENABLED", raising=False)
    monkeypatch.delenv("WELLNESS_CLAP_ENABLED", raising=False)
    return MemoryConfig(storage_path=str(tmp_path / "memory_data"))


def _build(config: MemoryConfig) -> PerceptualMemory:
    return PerceptualMemory(config)


def _add_image_memory(mem: PerceptualMemory, memory_id: str = "m-1", raw: str = "photo-a") -> MemoryItem:
    item = MemoryItem(
        id=memory_id,
        content="一张午餐照片",
        memory_type="perceptual",
        user_id="u1",
        timestamp=datetime(2026, 7, 20, 12, 0, 0),
        importance=0.6,
        metadata={"modality": "image", "raw_data": raw},
    )
    mem.add(item)
    return item


# ---------------------------------------------------------------- P3.0-2
class TestUpdateReindexesVector:
    """update() 必须真正把向量写回对应模态的集合。"""

    def test_update_reindexes_vector(self, perceptual_env):
        mem = _build(perceptual_env)
        _add_image_memory(mem)
        image_store = mem.vector_stores["image"]
        assert len(image_store.added) == 1, "add() 应写入一次向量"

        ok = mem.update("m-1", content="午餐照片：花生酱吐司", metadata={"modality": "image", "raw_data": "photo-b"})

        assert ok is True
        # 修复前这里恒为 1（AttributeError 被静默吞掉）。
        assert len(image_store.added) == 2, "update() 应重新索引一次向量"
        payload = image_store.added[-1]
        assert payload["ids"] == ["m-1"]
        assert payload["metadata"][0]["memory_id"] == "m-1"
        assert payload["metadata"][0]["modality"] == "image"
        assert payload["metadata"][0]["content"] == "午餐照片：花生酱吐司"

    def test_update_writes_to_modality_specific_store(self, perceptual_env):
        """向量必须落在 image 集合，不能串到 text 集合。"""
        mem = _build(perceptual_env)
        _add_image_memory(mem)
        text_baseline = len(mem.vector_stores["text"].added)

        mem.update("m-1", content="新的图片描述")

        assert len(mem.vector_stores["image"].added) == 2
        assert len(mem.vector_stores["text"].added) == text_baseline

    def test_update_vector_changes_when_raw_data_changes(self, perceptual_env):
        """原始数据变化时，重索引的向量应与首次不同。"""
        mem = _build(perceptual_env)
        _add_image_memory(mem, raw="photo-a")
        store = mem.vector_stores["image"]
        first_vector = store.added[0]["vectors"][0]

        mem.update("m-1", metadata={"modality": "image", "raw_data": "photo-completely-different"})

        assert len(store.added) == 2
        assert store.added[1]["vectors"][0] != first_vector

    def test_update_falls_back_to_cached_modality(self, perceptual_env):
        """metadata 未带 modality 时，应沿用缓存中的模态（仍写 image 集合）。"""
        mem = _build(perceptual_env)
        _add_image_memory(mem)

        mem.update("m-1", content="仅改内容，不带 modality")

        assert len(mem.vector_stores["image"].added) == 2

    def test_update_still_writes_doc_store(self, perceptual_env):
        """SQLite 权威更新不受向量逻辑影响。"""
        mem = _build(perceptual_env)
        _add_image_memory(mem)

        mem.update("m-1", content="c2", importance=0.9)

        assert mem.doc_store.update_calls[-1]["content"] == "c2"
        assert mem.doc_store.update_calls[-1]["importance"] == 0.9

    def test_update_unknown_memory_returns_false(self, perceptual_env):
        mem = _build(perceptual_env)
        assert mem.update("not-exist", content="x") is False

    def test_update_without_content_or_raw_data_skips_reindex(self, perceptual_env):
        """只改 importance 不该触发重索引（省一次编码）。"""
        mem = _build(perceptual_env)
        _add_image_memory(mem)

        mem.update("m-1", importance=0.95)

        assert len(mem.vector_stores["image"].added) == 1

    def test_class_has_no_singular_vector_store_attribute(self, perceptual_env):
        """守卫断言：本类不存在 vector_store（单数），防止 bug 回归。"""
        mem = _build(perceptual_env)
        assert not hasattr(mem, "vector_store")
        assert isinstance(mem.vector_stores, dict)


# ---------------------------------------------------------------- P3.0-1
class TestLazyEncoderLoading:
    """CLIP/CLAP 必须懒加载，且默认关闭。"""

    def test_construction_does_not_load_models(self, perceptual_env, monkeypatch):
        recorder = LoadRecorder()
        _install_fake_transformers(monkeypatch, recorder)

        mem = _build(perceptual_env)

        assert recorder.total == 0, "构造 PerceptualMemory 不得触发任何 from_pretrained"
        assert mem._clip_model is None
        assert mem._clap_model is None

    def test_defaults_are_disabled(self, perceptual_env):
        mem = _build(perceptual_env)
        assert mem._clip_enabled is False
        assert mem._clap_enabled is False

    def test_encoding_image_does_not_load_when_disabled(self, perceptual_env, monkeypatch):
        """开关关闭时，编码图片也不加载模型（走哈希向量）。"""
        recorder = LoadRecorder()
        _install_fake_transformers(monkeypatch, recorder)
        mem = _build(perceptual_env)

        vector = mem._encode_data("some-image-path", "image")

        assert recorder.total == 0
        assert len(vector) == mem.vector_dim  # 关闭时维度退化为文本维度

    def test_ensure_clip_returns_false_when_disabled(self, perceptual_env, monkeypatch):
        recorder = LoadRecorder()
        _install_fake_transformers(monkeypatch, recorder)
        mem = _build(perceptual_env)

        assert mem._ensure_clip() is False
        assert recorder.clip_calls == []

    def test_enabled_flag_loads_only_on_first_use(self, perceptual_env, monkeypatch):
        """开启开关后：构造仍不加载，首次编码才加载，且只加载一次。"""
        monkeypatch.setenv("WELLNESS_CLIP_ENABLED", "true")
        recorder = LoadRecorder()
        _install_fake_transformers(monkeypatch, recorder)

        mem = _build(perceptual_env)
        assert recorder.clip_calls == [], "构造期依然不加载"

        assert mem._ensure_clip() is True
        assert len(recorder.clip_calls) == 1
        assert mem._ensure_clip() is True
        assert len(recorder.clip_calls) == 1, "已加载后不应重复加载"

    def test_load_failure_is_not_retried(self, perceptual_env, monkeypatch):
        """加载失败只尝试一次，之后一直回退哈希（避免每张图都超时）。"""
        monkeypatch.setenv("WELLNESS_CLIP_ENABLED", "1")
        recorder = LoadRecorder()
        _install_fake_transformers(monkeypatch, recorder, fail=True)
        mem = _build(perceptual_env)

        assert mem._ensure_clip() is False
        assert mem._ensure_clip() is False
        assert len(recorder.clip_calls) == 1

    def test_clap_lazy_and_disabled_by_default(self, perceptual_env, monkeypatch):
        recorder = LoadRecorder()
        _install_fake_transformers(monkeypatch, recorder)
        mem = _build(perceptual_env)

        assert mem._ensure_clap() is False
        assert recorder.clap_calls == []

        mem._clap_enabled = True
        mem._clap_load_attempted = False
        assert mem._ensure_clap() is True
        assert len(recorder.clap_calls) == 1

    def test_collection_dim_follows_switch(self, perceptual_env, monkeypatch):
        """开关关闭 → 图像集合维度退化为文本维度；开启 → 用期望维度 512。"""
        mem_off = _build(perceptual_env)
        assert mem_off._image_dim == mem_off.vector_dim

        monkeypatch.setenv("WELLNESS_CLIP_ENABLED", "true")
        monkeypatch.setenv("WELLNESS_CLAP_ENABLED", "true")
        mem_on = _build(perceptual_env)
        assert mem_on._image_dim == 512
        assert mem_on._audio_dim == 512

    def test_clip_vector_dim_override(self, perceptual_env, monkeypatch):
        monkeypatch.setenv("WELLNESS_CLIP_ENABLED", "true")
        monkeypatch.setenv("CLIP_VECTOR_DIM", "768")
        mem = _build(perceptual_env)
        assert mem._image_dim == 768

    def test_hash_encoding_is_deterministic(self, perceptual_env):
        """关闭编码器时哈希向量必须稳定（跨实例可比）。"""
        mem_a = _build(perceptual_env)
        mem_b = _build(perceptual_env)
        assert mem_a._encode_data("同一份数据", "image") == mem_b._encode_data("同一份数据", "image")

    def test_env_flag_parsing(self, monkeypatch):
        monkeypatch.setenv("X_FLAG", "on")
        assert perceptual_module._env_flag("X_FLAG") is True
        monkeypatch.setenv("X_FLAG", "0")
        assert perceptual_module._env_flag("X_FLAG") is False
        monkeypatch.delenv("X_FLAG", raising=False)
        assert perceptual_module._env_flag("X_FLAG", True) is True

    def test_env_int_parsing(self, monkeypatch):
        monkeypatch.setenv("X_DIM", "1024")
        assert perceptual_module._env_int("X_DIM", 512) == 1024
        monkeypatch.setenv("X_DIM", "not-a-number")
        assert perceptual_module._env_int("X_DIM", 512) == 512

