"""core/message.py 单元测试。"""

from datetime import datetime

import pytest

from core.message import (
    MEDIA_PLACEHOLDER,
    MEDIA_URL_SCHEME,
    Message,
    is_media_ref,
    make_image_part,
    make_text_part,
    media_ref_id,
)


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

    def test_str_with_media(self):
        m = Message([make_text_part("这是什么"), make_image_part("f1")], "user")
        assert str(m) == f"[user] 这是什么 {MEDIA_PLACEHOLDER}"


# ================================================================== R9 多模态
# ------------------------------------------------------------------ 分片构造器
class TestContentPartHelpers:
    """``make_text_part`` / ``make_image_part`` / 引用解析。"""

    def test_make_text_part(self):
        assert make_text_part("hi") == {"type": "text", "text": "hi"}

    def test_make_image_part_default_detail(self):
        part = make_image_part("abc123")
        assert part["type"] == "image_url"
        assert part["image_url"]["url"] == f"{MEDIA_URL_SCHEME}abc123"
        assert part["image_url"]["detail"] == "auto"

    def test_make_image_part_custom_detail(self):
        part = make_image_part("abc123", detail="high")
        assert part["image_url"]["detail"] == "high"

    def test_make_image_part_never_contains_base64(self):
        """§2.2 硬约束：content 里绝不出现 base64 / data-url。"""
        part = make_image_part("abc123")
        serialized = str(part)
        assert "base64" not in serialized
        assert "data:image" not in serialized

    def test_is_media_ref_true(self):
        assert is_media_ref(f"{MEDIA_URL_SCHEME}x") is True

    def test_is_media_ref_false_for_http(self):
        assert is_media_ref("https://example.com/a.jpg") is False

    def test_is_media_ref_false_for_non_str(self):
        assert is_media_ref(None) is False
        assert is_media_ref(123) is False

    def test_media_ref_id_strips_scheme(self):
        assert media_ref_id(f"{MEDIA_URL_SCHEME}deadbeef") == "deadbeef"

    def test_media_ref_id_passthrough(self):
        assert media_ref_id("https://x/y.png") == "https://x/y.png"


# ------------------------------------------------------------------ C2 纯文本不变形
class TestConstraintC2TextNeverMorphs:
    """C2：纯文本一律存 ``str``，绝不规范化成 list。"""

    def test_plain_text_stays_str(self):
        m = Message("hello", "user")
        assert isinstance(m.content, str)
        assert m.content == "hello"

    def test_plain_text_to_openai_exact_shape(self):
        """一期精确断言必须原样通过。"""
        assert Message("hello", "user").to_openai() == {
            "role": "user",
            "content": "hello",
        }

    def test_text_only_list_is_flattened_to_str(self):
        m = Message([make_text_part("a"), make_text_part("b")], "user")
        assert isinstance(m.content, str)
        assert m.content == "a b"

    def test_text_only_list_to_openai_is_str(self):
        m = Message([make_text_part("only text")], "user")
        assert m.to_openai() == {"role": "user", "content": "only text"}

    def test_none_content_stays_none(self):
        m = Message(None, "user")
        assert m.content is None

    def test_to_dict_plain_text_unchanged(self):
        d = Message("hello", "user").to_dict()
        assert d["content"] == "hello"


# ------------------------------------------------------------------ C1 角色限制
class TestConstraintC1UserOnly:
    """C1：只有 ``user`` 角色允许 list content。"""

    def test_user_keeps_list(self):
        m = Message([make_text_part("看图"), make_image_part("f1")], "user")
        assert isinstance(m.content, list)
        assert len(m.content) == 2

    @pytest.mark.parametrize("role", ["assistant", "system", "tool", "summary"])
    def test_non_user_roles_flatten_to_str(self, role):
        m = Message([make_text_part("看图"), make_image_part("f1")], role)
        assert isinstance(m.content, str)
        assert m.content == f"看图 {MEDIA_PLACEHOLDER}"

    def test_tool_message_never_multimodal(self):
        """tool 消息的 API 不接受多模态。"""
        m = Message([make_image_part("f1")], "tool", tool_call_id="c1")
        assert isinstance(m.content, str)
        assert m.to_openai()["content"] == MEDIA_PLACEHOLDER

    def test_unknown_part_type_flattened_with_label(self):
        m = Message([{"type": "audio", "url": "x"}], "assistant")
        assert m.content == "[audio]"


# ------------------------------------------------------------------ 派生属性
class TestTextContent:
    def test_text_content_of_str(self):
        assert Message("hello", "user").text_content == "hello"

    def test_text_content_of_none(self):
        assert Message(None, "user").text_content == ""

    def test_text_content_of_list_joins_text_parts(self):
        m = Message(
            [make_text_part("这盘"), make_image_part("f1"), make_text_part("多少卡")],
            "user",
        )
        assert m.text_content == f"这盘 {MEDIA_PLACEHOLDER} 多少卡"

    def test_text_content_image_only(self):
        m = Message([make_image_part("f1")], "user")
        assert m.text_content == MEDIA_PLACEHOLDER

    def test_text_content_skips_empty_text_parts(self):
        m = Message([make_text_part(""), make_image_part("f1")], "user")
        assert m.text_content == MEDIA_PLACEHOLDER


class TestHasMedia:
    def test_false_for_str(self):
        assert Message("hello", "user").has_media is False

    def test_false_for_none(self):
        assert Message(None, "user").has_media is False

    def test_true_for_image_list(self):
        assert Message([make_image_part("f1")], "user").has_media is True

    def test_false_after_text_only_flatten(self):
        assert Message([make_text_part("a")], "user").has_media is False

    def test_false_for_flattened_non_user_role(self):
        assert Message([make_image_part("f1")], "assistant").has_media is False


class TestMediaRefs:
    def test_empty_for_str_content(self):
        assert Message("hello", "user").media_refs() == []

    def test_single_ref(self):
        m = Message([make_image_part("aaa")], "user")
        assert m.media_refs() == ["aaa"]

    def test_multiple_refs_in_order(self):
        m = Message(
            [make_image_part("a1"), make_text_part("和"), make_image_part("b2")],
            "user",
        )
        assert m.media_refs() == ["a1", "b2"]

    def test_ignores_external_urls(self):
        m = Message(
            [
                make_image_part("a1"),
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
            ],
            "user",
        )
        assert m.media_refs() == ["a1"]


# ------------------------------------------------------------------ C3 resolver
class TestConstraintC3Resolver:
    """C3：``resolver`` 缺省时 list content 降级为 ``text_content``。"""

    def test_no_resolver_degrades_to_text(self):
        m = Message([make_text_part("这是什么"), make_image_part("f1")], "user")
        d = m.to_openai()
        assert d == {"role": "user", "content": f"这是什么 {MEDIA_PLACEHOLDER}"}

    def test_resolver_expands_to_data_url(self):
        m = Message([make_text_part("这是什么"), make_image_part("f1")], "user")
        d = m.to_openai(resolver=lambda fid: f"data:image/jpeg;base64,BODY-{fid}")
        assert isinstance(d["content"], list)
        assert d["content"][0] == {"type": "text", "text": "这是什么"}
        assert d["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,BODY-f1"
        assert d["content"][1]["image_url"]["detail"] == "auto"

    def test_resolver_does_not_mutate_original_content(self):
        """解析必须走深拷贝，原 content 仍只存短引用。"""
        m = Message([make_image_part("f1")], "user")
        m.to_openai(resolver=lambda fid: "data:image/png;base64,ZZZ")
        assert m.content[0]["image_url"]["url"] == f"{MEDIA_URL_SCHEME}f1"

    def test_resolver_failure_degrades_to_placeholder(self):
        def boom(_fid):
            raise RuntimeError("file gone")

        m = Message([make_text_part("看"), make_image_part("f1")], "user")
        d = m.to_openai(resolver=boom)
        assert d["content"] == [
            {"type": "text", "text": "看"},
            {"type": "text", "text": MEDIA_PLACEHOLDER},
        ]

    def test_resolver_empty_result_degrades_to_placeholder(self):
        m = Message([make_image_part("f1")], "user")
        d = m.to_openai(resolver=lambda _fid: "")
        assert d["content"] == [{"type": "text", "text": MEDIA_PLACEHOLDER}]

    def test_resolver_ignored_for_str_content(self):
        called = []

        def resolver(fid):
            called.append(fid)
            return "x"

        assert Message("plain", "user").to_openai(resolver=resolver) == {
            "role": "user",
            "content": "plain",
        }
        assert called == []

    def test_resolver_leaves_external_urls_alone(self):
        m = Message(
            [
                make_image_part("a1"),
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
            ],
            "user",
        )
        d = m.to_openai(resolver=lambda fid: f"data:image/png;base64,{fid}")
        assert d["content"][0]["image_url"]["url"] == "data:image/png;base64,a1"
        assert d["content"][1]["image_url"]["url"] == "https://x/y.png"


# ------------------------------------------------------------------ 多模态落盘
class TestMultimodalSerialization:
    def test_to_dict_keeps_list_of_short_refs(self):
        m = Message([make_text_part("看"), make_image_part("sha256abc")], "user")
        d = m.to_dict()
        assert isinstance(d["content"], list)
        assert d["content"][1]["image_url"]["url"] == f"{MEDIA_URL_SCHEME}sha256abc"

    def test_to_dict_is_json_serializable(self):
        import json

        m = Message([make_text_part("看"), make_image_part("f1")], "user")
        assert json.loads(json.dumps(m.to_dict(), ensure_ascii=False))["content"]

    def test_roundtrip_preserves_media(self):
        original = Message([make_text_part("看"), make_image_part("f1")], "user")
        restored = Message.from_dict(original.to_dict())
        assert restored.has_media is True
        assert restored.media_refs() == ["f1"]
        assert restored.text_content == original.text_content

    def test_serialized_size_stays_tiny(self):
        """§2.2：单条带图消息落盘应在百字节量级，而非 MB 级 base64。"""
        import json

        m = Message([make_image_part("a" * 64)], "user")
        payload = json.dumps(m.to_dict(), ensure_ascii=False)
        assert len(payload) < 512
        assert "base64" not in payload

