"""core/session_store.py 单元测试。"""

import json
import tempfile
from pathlib import Path

import pytest

from core.message import Message
from core.session_store import SessionStore


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def store(tmp_dir):
    return SessionStore(session_dir=tmp_dir)


class TestSessionStoreIds:
    def test_new_session_id_format(self):
        sid = SessionStore.new_session_id()
        assert sid.startswith("s-")
        # 格式 s-YYYYMMDD-HHMMSS-xxxx
        parts = sid.split("-")
        assert len(parts) == 4

    def test_new_session_id_unique(self):
        ids = {SessionStore.new_session_id() for _ in range(20)}
        assert len(ids) == 20


class TestSessionStoreSaveLoad:
    def test_save_and_load(self, store, tmp_dir):
        msgs = [Message("hi", "user"), Message("hello", "assistant")]
        path = store.save("user1", "sess1", msgs)
        assert Path(path).exists()

        loaded = store.load("user1", "sess1")
        assert loaded is not None
        assert loaded["session_id"] == "sess1"
        assert loaded["user_id"] == "user1"
        assert len(loaded["messages"]) == 2
        assert loaded["messages"][0].content == "hi"

    def test_load_nonexistent(self, store):
        result = store.load("noone", "nosession")
        assert result is None

    def test_save_creates_user_dir(self, store, tmp_dir):
        msgs = [Message("hi", "user")]
        store.save("newuser", "sess1", msgs)
        assert (Path(tmp_dir) / "newuser").is_dir()

    def test_save_preserves_created_at(self, store):
        msgs = [Message("hi", "user")]
        store.save("user1", "sess1", msgs)
        first = store.load("user1", "sess1")
        assert first is not None
        created_at_first = first["created_at"]

        # 再 save，created_at 不变
        store.save("user1", "sess1", [Message("hi2", "user")])
        second = store.load("user1", "sess1")
        assert second is not None
        assert second["created_at"] == created_at_first

    def test_save_with_metadata(self, store):
        msgs = [Message("hi", "user")]
        store.save("user1", "sess1", msgs, metadata={"model": "gpt-4"})
        loaded = store.load("user1", "sess1")
        assert loaded is not None
        assert loaded["metadata"]["model"] == "gpt-4"

    def test_roundtrip_with_fc_fields(self, store):
        tc = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        msgs = [
            Message("hi", "user"),
            Message(None, "assistant", tool_calls=tc),
            Message("result", "tool", tool_call_id="c1", name="f"),
        ]
        store.save("user1", "sess1", msgs)
        loaded = store.load("user1", "sess1")
        assert loaded is not None
        restored = loaded["messages"]
        assert restored[1].tool_calls == tc
        assert restored[2].tool_call_id == "c1"
        assert restored[2].name == "f"

    def test_atomic_write(self, store, tmp_dir):
        """验证 .json.tmp 被替换为 .json。"""
        msgs = [Message("hi", "user")]
        store.save("user1", "sess1", msgs)
        user_dir = Path(tmp_dir) / "user1"
        tmp_files = list(user_dir.glob("*.tmp"))
        assert len(tmp_files) == 0  # tmp 不应残留
        json_files = list(user_dir.glob("*.json"))
        assert len(json_files) == 1


class TestSessionStoreListSessions:
    def test_list_empty(self, store):
        result = store.list_sessions("noone")
        assert result == []

    def test_list_multiple_sessions(self, store):
        for i in range(3):
            store.save("user1", f"sess{i}", [Message(f"q{i}", "user")])
        sessions = store.list_sessions("user1")
        assert len(sessions) == 3
        # 按 updated_at 倒序
        for s in sessions:
            assert "session_id" in s
            assert "message_count" in s
            assert "round_count" in s

    def test_latest_session_id(self, store):
        store.save("user1", "first", [Message("q1", "user")])
        store.save("user1", "second", [Message("q2", "user")])
        latest = store.latest_session_id("user1")
        assert latest is not None

    def test_latest_session_id_none(self, store):
        assert store.latest_session_id("noone") is None


class TestSessionStoreDelete:
    def test_delete_existing(self, store):
        store.save("user1", "sess1", [Message("hi", "user")])
        assert store.delete("user1", "sess1") is True
        assert store.load("user1", "sess1") is None

    def test_delete_nonexistent(self, store):
        assert store.delete("noone", "nosession") is False

