"""core/context.py 单元测试。"""

import os
import tempfile
from pathlib import Path

import pytest

from core.context import (
    DEFAULT_IMAGE_TOKEN_COST,
    HistoryManager,
    ObservationTruncator,
    TokenCounter,
    image_token_cost,
)
from core.message import MEDIA_PLACEHOLDER, Message, make_image_part, make_text_part


# ==================================================================
# TokenCounter
# ==================================================================
class TestTokenCounter:
    def test_count_text_nonempty(self):
        tc = TokenCounter()
        n = tc.count_text("Hello world")
        assert n > 0

    def test_count_text_empty(self):
        tc = TokenCounter()
        assert tc.count_text("") == 0

    def test_count_text_none_like(self):
        tc = TokenCounter()
        assert tc.count_text(None) == 0  # type: ignore

    def test_count_message_message_obj(self):
        tc = TokenCounter()
        m = Message("Hello world", "user")
        n = tc.count_message(m)
        assert n > 0

    def test_count_message_dict(self):
        tc = TokenCounter()
        m = {"role": "user", "content": "Hello"}
        n = tc.count_message(m)
        assert n > 0

    def test_count_message_with_tool_calls(self):
        tc = TokenCounter()
        m = Message("", "assistant", tool_calls=[{"id": "c1"}])
        n = tc.count_message(m)
        assert n > 0  # tool_calls 字符串也计 token

    def test_count_messages_total(self):
        tc = TokenCounter()
        msgs = [Message("a", "user"), Message("b", "assistant")]
        total = tc.count_messages(msgs)
        assert total > 0
        # count_messages 有 +2 固定开销
        assert total >= 4

    def test_cache_consistency(self):
        tc = TokenCounter()
        m = Message("cached text", "user")
        first = tc.count_message(m)
        second = tc.count_message(m)
        assert first == second

    def test_clear_cache(self):
        tc = TokenCounter()
        tc.count_message(Message("x", "user"))
        assert len(tc._cache) > 0
        tc.clear_cache()
        assert len(tc._cache) == 0

    def test_get_cache_stats(self):
        tc = TokenCounter()
        stats = tc.get_cache_stats()
        assert "cached_messages" in stats
        assert "tiktoken" in stats


# ==================================================================
# HistoryManager
# ==================================================================
class TestHistoryManager:
    def _make_round(self, user_text: str, assistant_text: str = "ok") -> list[Message]:
        return [Message(user_text, "user"), Message(assistant_text, "assistant")]

    def test_append_and_get(self):
        hm = HistoryManager()
        hm.append(Message("hi", "user"))
        assert len(hm.get_history()) == 1

    def test_extend(self):
        hm = HistoryManager()
        hm.extend([Message("a", "user"), Message("b", "assistant")])
        assert len(hm.get_history()) == 2

    def test_set_history(self):
        hm = HistoryManager()
        hm.append(Message("old", "user"))
        hm.set_history([Message("new", "user")])
        assert len(hm.get_history()) == 1
        assert hm.get_history()[0].content == "new"

    def test_clear(self):
        hm = HistoryManager()
        hm.append(Message("hi", "user"))
        hm.clear()
        assert hm.get_history() == []

    def test_get_history_returns_copy(self):
        hm = HistoryManager()
        hm.append(Message("hi", "user"))
        h = hm.get_history()
        h.append(Message("extra", "user"))
        assert len(hm.get_history()) == 1  # 原始不变

    def test_find_round_boundaries(self):
        hm = HistoryManager()
        hm.append(Message("sys", "system"))
        hm.append(Message("hi", "user"))
        hm.append(Message("ok", "assistant"))
        hm.append(Message("hello", "user"))
        hm.append(Message("hey", "assistant"))
        boundaries = hm.find_round_boundaries()
        assert boundaries == [1, 3]

    def test_estimate_rounds(self):
        hm = HistoryManager()
        hm.extend(self._make_round("q1") + self._make_round("q2"))
        assert hm.estimate_rounds() == 2

    def test_compress_no_need(self):
        hm = HistoryManager(min_retain_rounds=6)
        hm.extend(self._make_round("q1") + self._make_round("q2"))
        result = hm.compress("summary text")
        assert result is False

    def test_compress_happens(self):
        hm = HistoryManager(min_retain_rounds=2)
        # 4 rounds
        for i in range(4):
            hm.extend(self._make_round(f"q{i}"))
        result = hm.compress("early summary")
        assert result is True
        history = hm.get_history()
        # 应保留 system (0) + summary (1) + 最近 2 轮
        assert history[0].role == "summary"
        assert "early summary" in history[0].content

    def test_compress_preserves_system_head(self):
        hm = HistoryManager(min_retain_rounds=1)
        hm.append(Message("system prompt", "system"))
        for i in range(3):
            hm.extend(self._make_round(f"q{i}"))
        hm.compress("summary")
        history = hm.get_history()
        assert history[0].role == "system"
        assert history[0].content == "system prompt"

    def test_compress_keeps_recent_rounds_intact(self):
        hm = HistoryManager(min_retain_rounds=1)
        for i in range(3):
            hm.extend(self._make_round(f"q{i}"))
        hm.compress("summary")
        history = hm.get_history()
        # 最近的 user 应在历史中
        user_msgs = [m for m in history if m.role == "user"]
        assert len(user_msgs) >= 1
        assert user_msgs[-1].content == "q2"


# ==================================================================
# ObservationTruncator
# ==================================================================
class TestObservationTruncator:
    def test_short_output_not_truncated(self):
        trunc = ObservationTruncator(max_lines=100, max_bytes=10000)
        result = trunc.truncate("tool1", "short output")
        assert result["truncated"] is False
        assert result["preview"] == "short output"
        assert result["full_output_path"] is None

    def test_long_lines_truncated(self):
        trunc = ObservationTruncator(max_lines=5, max_bytes=999999)
        long_output = "\n".join([f"line {i}" for i in range(20)])
        result = trunc.truncate("tool1", long_output)
        assert result["truncated"] is True
        assert result["stats"]["original_lines"] == 20
        assert result["stats"]["kept_lines"] == 5
        assert result["full_output_path"] is not None

    def test_large_bytes_truncated(self):
        trunc = ObservationTruncator(max_lines=99999, max_bytes=100)
        large_output = "A" * 500
        result = trunc.truncate("tool1", large_output)
        assert result["truncated"] is True
        assert result["stats"]["original_bytes"] == 500

    def test_direction_tail(self):
        trunc = ObservationTruncator(max_lines=3, max_bytes=999999, direction="tail")
        lines = "\n".join([f"line {i}" for i in range(10)])
        result = trunc.truncate("tool1", lines)
        assert result["truncated"] is True
        assert "line 9" in result["preview"]

    def test_direction_head_tail(self):
        trunc = ObservationTruncator(max_lines=6, max_bytes=999999, direction="head_tail")
        lines = "\n".join([f"line {i}" for i in range(20)])
        result = trunc.truncate("tool1", lines)
        assert result["truncated"] is True
        assert "中间省略" in result["preview"]

    def test_empty_output(self):
        trunc = ObservationTruncator()
        result = trunc.truncate("tool1", "")
        assert result["truncated"] is False
        assert result["preview"] == ""

    def test_none_output(self):
        trunc = ObservationTruncator()
        result = trunc.truncate("tool1", None)
        assert result["truncated"] is False

    def test_full_output_saved_to_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trunc = ObservationTruncator(max_lines=3, max_bytes=999999, output_dir=tmpdir)
            long_output = "\n".join([f"line {i}" for i in range(20)])
            result = trunc.truncate("tool1", long_output)
            assert result["full_output_path"] is not None
            saved = Path(result["full_output_path"])
            assert saved.exists()
            assert long_output in saved.read_text(encoding="utf-8")

    def test_stats_fields(self):
        trunc = ObservationTruncator(max_lines=5, max_bytes=999999)
        output = "\n".join([f"line {i}" for i in range(20)])
        result = trunc.truncate("t", output)
        stats = result["stats"]
        assert "original_lines" in stats
        assert "kept_lines" in stats
        assert "original_bytes" in stats


# ==================================================================
# R9 多模态：TokenCounter 图片计数（方案 §2.3）
# ==================================================================
class TestTokenCounterMultimodal:
    def test_image_token_cost_default(self, monkeypatch):
        monkeypatch.delenv("WELLNESS_IMAGE_TOKEN_COST", raising=False)
        assert image_token_cost() == DEFAULT_IMAGE_TOKEN_COST == 1024

    def test_image_token_cost_from_env(self, monkeypatch):
        monkeypatch.setenv("WELLNESS_IMAGE_TOKEN_COST", "256")
        assert image_token_cost() == 256

    def test_image_token_cost_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("WELLNESS_IMAGE_TOKEN_COST", "abc")
        assert image_token_cost() == DEFAULT_IMAGE_TOKEN_COST

    def test_image_token_cost_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("WELLNESS_IMAGE_TOKEN_COST", "-5")
        assert image_token_cost() == DEFAULT_IMAGE_TOKEN_COST

    def test_image_adds_fixed_cost(self, monkeypatch):
        monkeypatch.setenv("WELLNESS_IMAGE_TOKEN_COST", "100")
        tc = TokenCounter()
        text_only = tc.count_message(Message("看看这个", "user"))
        with_image = tc.count_message(
            Message([make_text_part("看看这个"), make_image_part("f1")], "user")
        )
        assert with_image == text_only + 100

    def test_two_images_double_cost(self, monkeypatch):
        monkeypatch.setenv("WELLNESS_IMAGE_TOKEN_COST", "100")
        tc = TokenCounter()
        one = tc.count_message(Message([make_image_part("a")], "user"))
        two = tc.count_message(
            Message([make_image_part("a"), make_image_part("b")], "user")
        )
        assert two == one + 100

    def test_media_ref_not_counted_as_text(self, monkeypatch):
        """``wellness-file://<64位sha>`` 绝不能当文本进 tiktoken。"""
        monkeypatch.setenv("WELLNESS_IMAGE_TOKEN_COST", "0")
        tc = TokenCounter()
        short = tc.count_message(Message([make_image_part("a")], "user"))
        long = tc.count_message(Message([make_image_part("z" * 200)], "user"))
        assert short == long

    def test_cache_key_includes_image_count(self, monkeypatch):
        """不同图数不得命中同一缓存。"""
        monkeypatch.setenv("WELLNESS_IMAGE_TOKEN_COST", "100")
        tc = TokenCounter()
        one = tc.count_message(Message([make_text_part("x"), make_image_part("a")], "user"))
        two = tc.count_message(
            Message([make_text_part("x"), make_image_part("a"), make_image_part("b")], "user")
        )
        assert one != two

    def test_dict_message_with_list_content(self, monkeypatch):
        monkeypatch.setenv("WELLNESS_IMAGE_TOKEN_COST", "100")
        tc = TokenCounter()
        n = tc.count_message(
            {"role": "user", "content": [make_text_part("hi"), make_image_part("f1")]}
        )
        assert n >= 100

    def test_message_parts_returns_four_tuple(self):
        role, text, extra, images = TokenCounter._message_parts(
            Message([make_text_part("hi"), make_image_part("f1")], "user")
        )
        assert (role, text, extra, images) == ("user", "hi", "", 1)

    def test_message_parts_plain_text_zero_images(self):
        assert TokenCounter._message_parts(Message("hi", "user")) == ("user", "hi", "", 0)

    def test_split_content_none(self):
        assert TokenCounter._split_content(None) == ("", 0)

    def test_split_content_str(self):
        assert TokenCounter._split_content("abc") == ("abc", 0)

    def test_count_messages_includes_images(self, monkeypatch):
        monkeypatch.setenv("WELLNESS_IMAGE_TOKEN_COST", "100")
        tc = TokenCounter()
        total = tc.count_messages(
            [Message("hi", "user"), Message([make_image_part("f1")], "user")]
        )
        assert total > 100


# ==================================================================
# R9 多模态：HistoryManager.demote_media（方案 §2.3 阶梯中间档）
# ==================================================================
class TestDemoteMedia:
    @staticmethod
    def _rounds(count: int, image_at: set[int]) -> list[Message]:
        history: list[Message] = [Message("sys", "system")]
        for i in range(count):
            if i in image_at:
                history.append(
                    Message([make_text_part(f"q{i}"), make_image_part(f"img{i}")], "user")
                )
            else:
                history.append(Message(f"q{i}", "user"))
            history.append(Message(f"a{i}", "assistant"))
        return history

    def test_noop_when_within_retain_window(self):
        hm = HistoryManager(min_retain_rounds=6)
        hm.set_history(self._rounds(3, {0}))
        assert hm.demote_media() == 0
        assert hm.get_history()[1].has_media is True

    def test_demotes_only_outside_window(self):
        hm = HistoryManager(min_retain_rounds=2)
        hm.set_history(self._rounds(5, {0, 4}))
        assert hm.demote_media() == 1
        history = hm.get_history()
        assert history[1].has_media is False  # 旧轮已降级
        assert history[9].has_media is True   # 最近轮保留

    def test_demoted_message_becomes_str_content(self):
        """降级后只剩文本 part，C2 会把它拍平回 str。"""
        hm = HistoryManager(min_retain_rounds=1)
        hm.set_history(self._rounds(4, {0}))
        hm.demote_media()
        assert isinstance(hm.get_history()[1].content, str)

    def test_placeholder_without_vision_summary(self):
        hm = HistoryManager(min_retain_rounds=1)
        hm.set_history(self._rounds(4, {0}))
        hm.demote_media()
        assert MEDIA_PLACEHOLDER in hm.get_history()[1].text_content

    def test_uses_food_recognition_summary(self):
        """§2.3：占位文本优先取识餐摘要，旧图片不白白丢语义。"""
        hm = HistoryManager(min_retain_rounds=1)
        msg = Message(
            [make_text_part("这个多少卡"), make_image_part("imgX")],
            "user",
            metadata={"vision": {"imgX": {"summary": "花生酱吐司配牛奶"}}},
        )
        hm.set_history([msg] + [Message(f"q{i}", "user") for i in range(4)])
        assert hm.demote_media() == 1
        assert "[图片：花生酱吐司配牛奶]" in hm.get_history()[0].text_content

    def test_accepts_plain_string_summary_metadata(self):
        hm = HistoryManager(min_retain_rounds=1)
        msg = Message(
            [make_image_part("imgX")], "user", metadata={"vision": {"imgX": "牛肉面"}}
        )
        hm.set_history([msg] + [Message(f"q{i}", "user") for i in range(4)])
        hm.demote_media()
        assert "[图片：牛肉面]" in hm.get_history()[0].text_content

    def test_preserves_text_parts(self):
        hm = HistoryManager(min_retain_rounds=1)
        hm.set_history(self._rounds(4, {0}))
        hm.demote_media()
        assert "q0" in hm.get_history()[1].text_content

    def test_preserves_metadata_and_role(self):
        hm = HistoryManager(min_retain_rounds=1)
        msg = Message([make_image_part("i")], "user", metadata={"k": "v"})
        hm.set_history([msg] + [Message(f"q{i}", "user") for i in range(4)])
        hm.demote_media()
        restored = hm.get_history()[0]
        assert restored.role == "user" and restored.metadata["k"] == "v"

    def test_retain_zero_demotes_everything(self):
        hm = HistoryManager(min_retain_rounds=6)
        hm.set_history(self._rounds(2, {0, 1}))
        assert hm.demote_media(retain_rounds=0) == 2

    def test_explicit_retain_rounds_override(self):
        hm = HistoryManager(min_retain_rounds=6)
        hm.set_history(self._rounds(5, {0}))
        assert hm.demote_media(retain_rounds=1) == 1

    def test_idempotent(self):
        hm = HistoryManager(min_retain_rounds=1)
        hm.set_history(self._rounds(4, {0}))
        assert hm.demote_media() == 1
        assert hm.demote_media() == 0

    def test_no_media_history_is_noop(self):
        hm = HistoryManager(min_retain_rounds=1)
        hm.set_history(self._rounds(4, set()))
        assert hm.demote_media() == 0

    def test_reduces_token_count(self, monkeypatch):
        monkeypatch.setenv("WELLNESS_IMAGE_TOKEN_COST", "1024")
        tc = TokenCounter()
        hm = HistoryManager(min_retain_rounds=1)
        hm.set_history(self._rounds(4, {0, 1}))
        before = tc.count_messages(hm.get_history())
        hm.demote_media()
        assert tc.count_messages(hm.get_history()) < before - 1024

