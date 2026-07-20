"""core/message.py 单元测试。"""

from datetime import datetime

import pytest

from core.message import Message


# ------------------------------------------------------------------ 构造
class TestMessageConstruction:
    """Message 构造兼容性测试。"""

    def test_positional_args(self):
        m = Message("hello", "user")
        assert m.content == "hello"
        assert m.role == "user"

    def test_keyword_args(self):
        m = Message(content="hi", role="assistant")
        assert m.content == "hi"
        assert m.role == "assistant"

    def test_default_role_is_user(self):
        m = Message("x")
        assert m.role == "user"

    def test_none_content(self):
        m = Message(None, "assistant")
        assert m.content is None

    def test_timestamp_auto_generated(self):
        m = Message("x", "user")
        assert isinstance(m.timestamp, datetime)

    def test_custom_timestamp(self):
        ts = datetime(2025, 1, 1, 0, 0, 0)
        m = Message("x", "user", timestamp=ts)
        assert m.timestamp == ts

    def test_metadata_default_empty_dict(self):
        m = Message("x", "user")
        assert m.metadata == {}

    def test_custom_metadata(self):
        m = Message("x", "user", metadata={"k": 1})
        assert m.metadata == {"k": 1}

    def test_tool_calls_field(self):
        tc = [{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]
        m = Message("", "assistant", tool_calls=tc)
        assert m.tool_calls == tc

    def test_tool_call_id_field(self):
        m = Message("result", "tool", tool_call_id="call_1")
        assert m.tool_call_id == "call_1"

    def test_name_field(self):
        m = Message("result", "tool", name="search")
        assert m.name == "search"


# ------------------------------------------------------------------ to_openai
class TestMessageToOpenAI:
    """to_openai 转换测试。"""

    def test_user_message(self):
        m = Message("hello", "user")
        d = m.to_openai()
        assert d == {"role": "user", "content": "hello"}

    def test_system_message(self):
        m = Message("sys", "system")
        d = m.to_openai()
        assert d == {"role": "system", "content": "sys"}

    def test_assistant_with_tool_calls(self):
        tc = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        m = Message(None, "assistant", tool_calls=tc)
        d = m.to_openai()
        assert d["role"] == "assistant"
        assert d["tool_calls"] == tc
        assert d["content"] is None  # content 可为 None

    def test_assistant_without_tool_calls(self):
        m = Message("hi", "assistant")
        d = m.to_openai()
        assert d["content"] == "hi"
        assert "tool_calls" not in d

    def test_tool_message_with_id_and_name(self):
        m = Message("result", "tool", tool_call_id="c1", name="search")
        d = m.to_openai()
        assert d["role"] == "tool"
        assert d["tool_call_id"] == "c1"
        assert d["name"] == "search"

    def test_tool_message_without_optional_fields(self):
        m = Message("result", "tool")
        d = m.to_openai()
        assert "tool_call_id" not in d
        assert "name" not in d

    def test_none_content_becomes_empty_string_for_non_assistant(self):
        m = Message(None, "user")
        d = m.to_openai()
        assert d["content"] == ""


# ------------------------------------------------------------------ to_dict / from_dict
class TestMessageSerialization:
    """落盘序列化与反序列化测试。"""

    def test_to_dict_basic(self):
        m = Message("hello", "user")
        d = m.to_dict()
        assert d["role"] == "user"
        assert d["content"] == "hello"
        assert isinstance(d["timestamp"], str)
        assert d["tool_calls"] is None
        assert d["tool_call_id"] is None
        assert d["name"] is None
        assert d["metadata"] == {}

    def test_to_dict_with_fc_fields(self):
        tc = [{"id": "c1"}]
        m = Message("", "assistant", tool_calls=tc, tool_call_id="c1", name="f")
        d = m.to_dict()
        assert d["tool_calls"] == tc
        assert d["tool_call_id"] == "c1"
        assert d["name"] == "f"

    def test_roundtrip_simple(self):
        original = Message("ping", "user", metadata={"k": "v"})
        data = original.to_dict()
        restored = Message.from_dict(data)
        assert restored.content == original.content
        assert restored.role == original.role
        assert restored.metadata == original.metadata

    def test_roundtrip_with_tool_calls(self):
        tc = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        original = Message(None, "assistant", tool_calls=tc)
        data = original.to_dict()
        restored = Message.from_dict(data)
        assert restored.role == "assistant"
        assert restored.tool_calls == tc

    def test_roundtrip_tool_message(self):
        original = Message("result", "tool", tool_call_id="c1", name="search")
        data = original.to_dict()
        restored = Message.from_dict(data)
        assert restored.tool_call_id == "c1"
        assert restored.name == "search"
        assert restored.content == "result"

    def test_from_dict_timestamp_string(self):
        ts = "2025-06-15T12:00:00"
        data = {"role": "user", "content": "hi", "timestamp": ts}
        m = Message.from_dict(data)
        assert isinstance(m.timestamp, datetime)
        assert m.timestamp.year == 2025

    def test_from_dict_timestamp_invalid_string(self):
        data = {"role": "user", "content": "hi", "timestamp": "not-a-date"}
        m = Message.from_dict(data)
        # 降级为 datetime.now()
        assert isinstance(m.timestamp, datetime)

    def test_from_dict_timestamp_datetime_object(self):
        ts = datetime(2025, 1, 1)
        data = {"role": "user", "content": "hi", "timestamp": ts}
        m = Message.from_dict(data)
        assert m.timestamp == ts

    def test_from_dict_missing_timestamp(self):
        data = {"role": "user", "content": "hi"}
        m = Message.from_dict(data)
        assert isinstance(m.timestamp, datetime)

    def test_from_dict_metadata_none(self):
        data = {"role": "user", "content": "hi", "metadata": None}
        m = Message.from_dict(data)
        assert m.metadata == {}

    def test_from_dict_defaults(self):
        data = {}
        m = Message.from_dict(data)
        assert m.role == "user"
        assert m.content is None


# ------------------------------------------------------------------ __str__
class TestMessageStr:
    def test_str(self):
        m = Message("hello", "user")
        assert str(m) == "[user] hello"

