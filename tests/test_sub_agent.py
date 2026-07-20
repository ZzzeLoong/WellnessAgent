"""agents/sub_agent.py + wellnessagent/subagents 单测（R6-2 / R6-3）。

用 fake FC LLM 驱动，验证：
- SubAgent 隔离执行，只用授权工具，返回结构化摘要。
- 未授权工具被 registry 拒绝（权限隔离）。
- 专职 SubAgent 的授权集合符合 §2.3；profile 只读。
- 子代理执行异常被隔离为 success=False 结果。
- 子 trace 事件带 subagent 标签。
"""

import json
import uuid

from WellnessAgent.agents.sub_agent import SubAgent, SubAgentResult
from WellnessAgent.core.llm_response import LLMToolResponse, ToolCall
from WellnessAgent.tools.registry import ToolRegistry
from WellnessAgent.tools.tool_filter import WhitelistFilter, ReadOnlyFilter
from WellnessAgent.wellnessagent.subagents import (
    build_subagent,
    make_subagent_factory,
    SUBAGENT_SPECS,
)


def _tc(name, arguments, call_id=None):
    return ToolCall(id=call_id or f"call_{uuid.uuid4().hex[:8]}", name=name,
                    arguments=json.dumps(arguments, ensure_ascii=False))


class FakeFCLLM:
    def __init__(self, script):
        self.model = "fake-fc"
        self.provider = "openai"
        self._script = script
        self._idx = 0
        self.last_tools = None

    def supports_function_calling(self):
        return True

    def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
        self.last_tools = tools
        if self._idx >= len(self._script):
            return LLMToolResponse(content="", tool_calls=[], model=self.model)
        resp = self._script[self._idx]
        self._idx += 1
        return resp


def _registry():
    reg = ToolRegistry()
    for name in ("profile_get", "profile_set", "memory_search", "memory_remember",
                 "kb_search", "kb_answer", "kb_status", "session_recall", "session_note"):
        reg.register_function(name, f"{name} desc", (lambda n: (lambda x=None, **k: f"{n}:ok"))(name))
    return reg


class TestSubAgentExecute:
    def test_direct_finish_summary(self):
        llm = FakeFCLLM([LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "结论摘要"})], model="fake-fc")])
        sa = SubAgent("profile", llm, _registry(), WhitelistFilter({"profile_get"}), "系统提示")
        res = sa.execute("整理画像", {"画像上下文": "过敏花生"})
        assert isinstance(res, SubAgentResult)
        assert res.success is True
        assert res.summary == "结论摘要"
        assert res.metadata["terminated_reason"] == "finished"

    def test_only_allowed_tools_in_schema(self):
        llm = FakeFCLLM([LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "ok"})], model="fake-fc")])
        sa = SubAgent("retrieval", llm, _registry(), WhitelistFilter({"kb_search"}), "系统提示")
        sa.execute("检索", {})
        names = {t["function"]["name"] for t in llm.last_tools}
        assert names == {"kb_search", "finish"}

    def test_unauthorized_tool_rejected(self):
        llm = FakeFCLLM([
            LLMToolResponse(content=None, tool_calls=[_tc("profile_set", {"input": "x"})], model="fake-fc"),
            LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "done"})], model="fake-fc"),
        ])
        sa = SubAgent("profile", llm, _registry(), WhitelistFilter({"profile_get"}), "系统提示")
        res = sa.execute("试图写画像", {})
        tool_result = res.steps[0]["tool_results"][0]
        assert tool_result["status"] == "error"
        assert "未授权" in tool_result["content"]

    def test_context_injected_into_system(self):
        captured = {}

        class CapturingLLM(FakeFCLLM):
            def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
                captured["system"] = messages[0]["content"]
                return super().invoke_with_tools(messages, tools, tool_choice, **kwargs)

        llm = CapturingLLM([LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "ok"})], model="fake-fc")])
        sa = SubAgent("planning", llm, _registry(), WhitelistFilter({"kb_search"}), "规划系统提示")
        sa.execute("规划", {"画像上下文": "素食且过敏花生"})
        assert "规划系统提示" in captured["system"]
        assert "素食且过敏花生" in captured["system"]

    def test_execute_exception_isolated(self):
        class BoomLLM:
            model = "boom"
            provider = "openai"

            def supports_function_calling(self):
                return True

            def invoke_with_tools(self, *a, **k):
                raise RuntimeError("llm down")

        sa = SubAgent("safety", BoomLLM(), _registry(), WhitelistFilter(set()), "系统提示")
        res = sa.execute("审查", {})
        assert res.success is False
        assert "异常" in res.summary


class TestSubAgentTrace:
    def test_subagent_tag_forwarded(self):
        events = []

        class FakeTrace:
            def log_event(self, event, payload=None, step=None):
                events.append((event, payload))

        llm = FakeFCLLM([
            LLMToolResponse(content=None, tool_calls=[_tc("kb_search", {"input": "x"})], model="fake-fc"),
            LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "ok"})], model="fake-fc"),
        ])
        sa = SubAgent("retrieval", llm, _registry(), WhitelistFilter({"kb_search"}), "提示", parent_trace=FakeTrace())
        sa.execute("检索", {})
        # 子代理内部事件应带 subagent 标签。
        assert any(p.get("subagent") == "retrieval" for _, p in events if p)


class TestSubAgentFactory:
    def test_specs_authorizations(self):
        assert SUBAGENT_SPECS["profile"]["allowed"] == {"profile_get", "memory_search"}
        assert SUBAGENT_SPECS["retrieval"]["allowed"] == {"kb_search", "kb_answer", "kb_status"}
        assert SUBAGENT_SPECS["planning"]["allowed"] == {"kb_search", "session_recall"}
        assert SUBAGENT_SPECS["safety"]["allowed"] == set()

    def test_profile_is_readonly(self):
        reg = _registry()
        sa = build_subagent("profile", FakeFCLLM([]), reg)
        allowed = set(sa.tool_filter.filter(reg.list_tools()))
        # profile 只读：不含任何写工具。
        assert "profile_set" not in allowed
        assert "memory_remember" not in allowed
        assert allowed == {"profile_get", "memory_search"}

    def test_factory_builds_named_subagents(self):
        reg = _registry()
        factory = make_subagent_factory(FakeFCLLM([]), reg)
        for name in ("profile", "retrieval", "planning", "safety"):
            sa = factory(name)
            assert sa.name == name

    def test_unknown_subagent_raises(self):
        import pytest
        with pytest.raises(ValueError):
            build_subagent("unknown", FakeFCLLM([]), _registry())

