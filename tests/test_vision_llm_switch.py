"""R9 \u53cc\u6a21\u578b\u5207\u6362\u4e0e\u5a92\u4f53 resolver \u5355\u6d4b\uff08\u65b9\u6848 \u00a72.4\uff09\u3002

\u6838\u5fc3\u56de\u5f52\u7ea2\u7ebf\uff1a**\u4e0d\u542b\u56fe\u7684\u5bf9\u8bdd\u5fc5\u987b\u8d70\u539f\u6765\u90a3\u6761\u8def\u5f84\uff0c\u4e0e\u4e8c\u671f\u5b57\u8282\u7ea7\u7b49\u4ef7**\uff1a
\u540c\u4e00\u4e2a ``self.llm``\u3001\u540c\u4e00\u4efd ``str`` content payload\u3001\u4e0d\u78b0 resolver\u3002
"""

import pytest

from WellnessAgent.agents.react_agent import ReActAgent
from WellnessAgent.core.llm import HelloAgentsLLM
from WellnessAgent.core.llm_response import LLMToolResponse
from WellnessAgent.core.message import (
    MEDIA_PLACEHOLDER,
    Message,
    make_image_part,
    make_text_part,
)
from WellnessAgent.tools.registry import ToolRegistry


# ------------------------------------------------------------------ \u5047 LLM
class FakeLLM:
    """\u8bb0\u5f55\u6536\u5230\u7684 messages\uff0c\u76f4\u63a5\u7ed9\u6700\u7ec8\u7b54\u6848\u3002"""

    def __init__(self, name="main", model="fake-model"):
        self.name = name
        self.model = model
        self.seen_messages = []
        self.call_count = 0

    def supports_function_calling(self):
        return True

    def supports_vision(self):
        return self.name == "vision"

    def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
        self.call_count += 1
        self.seen_messages.append(messages)
        return LLMToolResponse(
            content=f"answer from {self.name}", tool_calls=[], model=self.model
        )

    def invoke(self, messages, **kwargs):
        self.call_count += 1
        self.seen_messages.append(messages)
        return f"answer from {self.name}"


def _agent(llm=None) -> ReActAgent:
    return ReActAgent(name="t", llm=llm or FakeLLM(), tool_registry=ToolRegistry())


# ================================================================== supports_vision
class TestSupportsVision:
    @staticmethod
    def _llm(model, provider, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "k")
        monkeypatch.setenv("LLM_BASE_URL", "https://example.com/v1")
        return HelloAgentsLLM(model=model, provider=provider, api_key="k",
                              base_url="https://example.com/v1")

    def test_force_vision_true_overrides(self, monkeypatch):
        monkeypatch.setenv("WELLNESS_FORCE_VISION", "true")
        llm = self._llm("gpt-3.5-turbo", "deepseek", monkeypatch)
        assert llm.supports_vision() is True

    def test_force_vision_false_overrides(self, monkeypatch):
        monkeypatch.setenv("WELLNESS_FORCE_VISION", "false")
        llm = self._llm("qwen-vl-max-latest", "qwen", monkeypatch)
        assert llm.supports_vision() is False

    @pytest.mark.parametrize(
        "model",
        ["qwen-vl-max-latest", "qwen-vl-plus", "gpt-4o", "glm-4v", "llava-1.5"],
    )
    def test_vision_model_names_detected(self, model, monkeypatch):
        monkeypatch.delenv("WELLNESS_FORCE_VISION", raising=False)
        assert self._llm(model, "custom", monkeypatch).supports_vision() is True

    @pytest.mark.parametrize("model", ["gpt-3.5-turbo", "deepseek-chat"])
    def test_non_vision_model_names_rejected(self, model, monkeypatch):
        monkeypatch.delenv("WELLNESS_FORCE_VISION", raising=False)
        assert self._llm(model, "openai", monkeypatch).supports_vision() is False

    def test_provider_whitelist_fallback(self, monkeypatch):
        monkeypatch.delenv("WELLNESS_FORCE_VISION", raising=False)
        assert self._llm("qwen-plus", "qwen", monkeypatch).supports_vision() is True

    def test_provider_outside_whitelist(self, monkeypatch):
        monkeypatch.delenv("WELLNESS_FORCE_VISION", raising=False)
        assert self._llm("llama3.2", "ollama", monkeypatch).supports_vision() is False

    def test_independent_from_function_calling(self, monkeypatch):
        """deepseek \u652f\u6301 FC \u4f46\u4e0d\u652f\u6301\u89c6\u89c9\uff0c\u4e24\u4e2a\u63a2\u6d4b\u4e92\u4e0d\u5e72\u6270\u3002"""
        monkeypatch.delenv("WELLNESS_FORCE_VISION", raising=False)
        monkeypatch.delenv("WELLNESS_FORCE_FUNCTION_CALLING", raising=False)
        llm = self._llm("deepseek-chat", "deepseek", monkeypatch)
        assert llm.supports_function_calling() is True
        assert llm.supports_vision() is False


# ================================================================== \u672c\u8f6e\u5207\u6362
class TestSelectLLMForTurn:
    def test_base_always_returns_main_llm(self):
        agent = _agent()
        assert agent._select_llm_for_turn() is agent.llm

    def test_has_media_false_for_text_history(self):
        agent = _agent()
        agent.messages = [Message("sys", "system"), Message("hi", "user")]
        assert agent._current_turn_has_media() is False

    def test_has_media_true_with_image_part(self):
        agent = _agent()
        agent.messages = [Message([make_image_part("f1")], "user")]
        assert agent._current_turn_has_media() is True

    def test_override_switches_on_media(self):
        """\u4e1a\u52a1 agent \u8986\u5199\u94a9\u5b50\u5373\u53ef\u5207\u6a21\u578b\uff0c\u4e0d\u52a8\u4e3b\u5faa\u73af\u7ed3\u6784\u3002"""
        main, vision = FakeLLM("main"), FakeLLM("vision")

        class Switching(ReActAgent):
            def _select_llm_for_turn(self):
                return vision if self._current_turn_has_media() else self.llm

        agent = Switching(name="t", llm=main, tool_registry=ToolRegistry())
        agent.run_with_trace("\u7eaf\u6587\u672c")
        assert main.call_count == 1 and vision.call_count == 0

        agent.run_with_trace(
            "\u8fd9\u76d8\u591a\u5c11\u5361",
            user_message=Message([make_image_part("f1")], "user"),
        )
        assert vision.call_count == 1


# ================================================================== resolver \u63a5\u5165
class TestMediaResolverWiring:
    def test_no_resolver_keeps_str_payload(self):
        """\u4e0d\u6ce8\u5165 resolver \u65f6\uff0cpayload \u4e0e\u4e00/\u4e8c\u671f\u5b8c\u5168\u4e00\u81f4\u3002"""
        llm = FakeLLM()
        agent = _agent(llm)
        agent.run_with_trace("hello")
        payload = llm.seen_messages[0]
        assert payload[-1] == {"role": "user", "content": "hello"}

    def test_default_resolver_is_none(self):
        assert _agent().media_resolver is None

    def test_resolver_expands_media_in_payload(self):
        llm = FakeLLM()
        agent = _agent(llm)
        agent.media_resolver = lambda fid: f"data:image/png;base64,B-{fid}"
        agent.run_with_trace(
            "\u770b\u56fe", user_message=Message([make_text_part("\u770b\u56fe"), make_image_part("f1")], "user")
        )
        content = llm.seen_messages[0][-1]["content"]
        assert content[1]["image_url"]["url"] == "data:image/png;base64,B-f1"

    def test_resolver_not_called_for_text_only(self):
        calls = []
        llm = FakeLLM()
        agent = _agent(llm)
        agent.media_resolver = lambda fid: calls.append(fid) or "x"
        agent.run_with_trace("\u7eaf\u6587\u672c")
        assert calls == []

    def test_stored_messages_never_hold_base64(self):
        """\u7ea2\u7ebf\uff1abase64 \u53ea\u5b58\u5728\u4e8e\u4e34\u65f6 payload\uff0c\u4e0d\u56de\u5199 messages\u3002"""
        import json

        llm = FakeLLM()
        agent = _agent(llm)
        agent.media_resolver = lambda fid: "data:image/png;base64," + "Q" * 2000
        agent.run_with_trace(
            "\u770b\u56fe", user_message=Message([make_image_part("f1")], "user")
        )
        dumped = json.dumps([m.to_dict() for m in agent.messages], ensure_ascii=False)
        assert "base64" not in dumped
        assert "wellness-file://f1" in dumped

    def test_failing_resolver_degrades_not_crashes(self):
        def boom(_fid):
            raise RuntimeError("gone")

        llm = FakeLLM()
        agent = _agent(llm)
        agent.media_resolver = boom
        result = agent.run_with_trace(
            "\u770b\u56fe", user_message=Message([make_image_part("f1")], "user")
        )
        assert result.final_answer == "answer from main"
        assert llm.seen_messages[0][-1]["content"] == [
            {"type": "text", "text": MEDIA_PLACEHOLDER}
        ]


# ================================================================== user_message \u5165\u53e3
class TestUserMessageParam:
    def test_default_builds_plain_text_message(self):
        agent = _agent()
        agent.run_with_trace("hello")
        user_msgs = [m for m in agent.messages if m.role == "user"]
        assert user_msgs[0].content == "hello"

    def test_custom_user_message_is_appended(self):
        agent = _agent()
        agent.run_with_trace(
            "\u770b\u56fe", user_message=Message([make_text_part("\u770b\u56fe"), make_image_part("f1")], "user")
        )
        user_msgs = [m for m in agent.messages if m.role == "user"]
        assert user_msgs[0].media_refs() == ["f1"]

    def test_stream_run_accepts_user_message(self):
        agent = _agent()
        events = list(
            agent.stream_run("\u770b\u56fe", user_message=Message([make_image_part("f1")], "user"))
        )
        assert any(e["type"] == "result" for e in events)
        assert agent.messages[1].has_media is True

    def test_trace_logs_text_not_media(self):
        """trace \u91cc\u53ea\u80fd\u51fa\u73b0\u6587\u672c\uff0c\u4e0d\u80fd\u51fa\u73b0 base64\u3002"""

        class Recorder:
            def __init__(self):
                self.events = []

            def log_event(self, event, payload=None, step=None):
                self.events.append((event, payload))

        agent = _agent()
        agent.trace_logger = Recorder()
        agent.media_resolver = lambda fid: "data:image/png;base64,ZZZ"
        agent.run_with_trace(
            "\u8fd9\u76d8\u591a\u5c11\u5361", user_message=Message([make_image_part("f1")], "user")
        )
        written = [p for e, p in agent.trace_logger.events if e == "message_written"]
        assert written[0]["content"] == "\u8fd9\u76d8\u591a\u5c11\u5361"
        assert "base64" not in str(agent.trace_logger.events)

