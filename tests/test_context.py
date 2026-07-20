"""core/context.py 单元测试。"""

import os
import tempfile
from pathlib import Path

import pytest

from core.context import TokenCounter, HistoryManager, ObservationTruncator
from core.message import Message


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

