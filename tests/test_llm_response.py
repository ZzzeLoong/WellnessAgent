"""core/llm_response.py 单元测试。"""

from core.llm_response import ToolCall, LLMToolResponse


class TestToolCall:
    def test_fields(self):
        tc = ToolCall(id="c1", name="search", arguments='{"q": "test"}')
        assert tc.id == "c1"
        assert tc.name == "search"
        assert tc.arguments == '{"q": "test"}'

    def test_empty_arguments(self):
        tc = ToolCall(id="c2", name="calc", arguments="{}")
        assert tc.arguments == "{}"


class TestLLMToolResponse:
    def test_basic_construction(self):
        tc = ToolCall(id="c1", name="search", arguments="{}")
        resp = LLMToolResponse(content="hello", tool_calls=[tc], model="gpt-4")
        assert resp.content == "hello"
        assert len(resp.tool_calls) == 1
        assert resp.model == "gpt-4"
        assert resp.usage == {}
        assert resp.latency_ms == 0

    def test_no_tool_calls(self):
        resp = LLMToolResponse(content="hi", tool_calls=[], model="gpt-4")
        assert resp.tool_calls == []

    def test_with_usage_and_latency(self):
        resp = LLMToolResponse(
            content="",
            tool_calls=[],
            model="gpt-4",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            latency_ms=150,
        )
        assert resp.usage["prompt_tokens"] == 10
        assert resp.latency_ms == 150

    def test_none_content(self):
        resp = LLMToolResponse(content=None, tool_calls=[], model="gpt-4")
        assert resp.content is None

    def test_multiple_tool_calls(self):
        tc1 = ToolCall(id="c1", name="search", arguments="{}")
        tc2 = ToolCall(id="c2", name="calc", arguments="{}")
        resp = LLMToolResponse(content=None, tool_calls=[tc1, tc2], model="gpt-4")
        assert len(resp.tool_calls) == 2
        assert resp.tool_calls[0].name == "search"
        assert resp.tool_calls[1].name == "calc"

