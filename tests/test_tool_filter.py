"""tools/tool_filter.py 单测（R6-1）。

ToolFilter 职责：计算某 SubAgent 的授权白名单集合（filter() 产出 allowed）。
"""

from WellnessAgent.tools.tool_filter import (
    ToolFilter,
    WhitelistFilter,
    ReadOnlyFilter,
)


class TestWhitelistFilter:
    def test_only_allowed_pass(self):
        f = WhitelistFilter({"kb_search", "kb_answer"})
        assert f.is_allowed("kb_search")
        assert not f.is_allowed("profile_set")

    def test_filter_returns_subset(self):
        f = WhitelistFilter({"kb_search"})
        result = f.filter(["kb_search", "kb_answer", "profile_set"])
        assert result == ["kb_search"]

    def test_empty_whitelist_allows_nothing(self):
        f = WhitelistFilter(set())
        assert f.filter(["a", "b"]) == []


class TestReadOnlyFilter:
    def test_denies_write_tools(self):
        f = ReadOnlyFilter()
        for name in ("profile_set", "profile_remove", "memory_remember", "session_note"):
            assert not f.is_allowed(name)

    def test_allows_read_tools(self):
        f = ReadOnlyFilter()
        assert f.is_allowed("profile_get")
        assert f.is_allowed("kb_search")

    def test_additional_denied(self):
        f = ReadOnlyFilter(additional_denied=["dangerous"])
        assert not f.is_allowed("dangerous")

    def test_filter_removes_write_tools(self):
        f = ReadOnlyFilter()
        result = f.filter(["profile_get", "profile_set", "kb_search", "memory_remember"])
        assert set(result) == {"profile_get", "kb_search"}


class TestIsToolFilterSubclass:
    def test_whitelist_is_tool_filter(self):
        assert isinstance(WhitelistFilter({"x"}), ToolFilter)

    def test_readonly_is_tool_filter(self):
        assert isinstance(ReadOnlyFilter(), ToolFilter)

