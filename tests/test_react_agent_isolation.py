"""ReActAgent 隔离运行入口单测（Phase 2.0 · P0-1 / P0-2）。

P0-1：run_with_trace/stream_run 的 system_prompt_override + allowed_tools。
P0-2：_finalize 不再写基类 _history；对话历史收敛到 self.messages。
"""

import json
import uuid

from WellnessAgent.agents.react_agent import ReActAgent, TERMINATED_FINISHED
from WellnessAgent.core.llm_response import LLMToolResponse, ToolCall
from WellnessAgent.tools.registry import ToolRegistry


def _tc(name, arguments, call_id=None):
    return ToolCall(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        name=name,
        arguments=json.dumps(arguments, ensure_ascii=False),
    )


class FakeFCLLM:
    def __init__(self, script, model="fake-fc"):
        self.model = model
        self.provider = "openai"
        self._script = script
        self._idx = 0
        self.last_messages = None
        self.last_tools = None

    def supports_function_calling(self):
        return True

    def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
        self.last_messages = messages
        self.last_tools = tools
        if self._idx >= len(self._script):
            return LLMToolResponse(content="", tool_calls=[], model=self.model)
        resp = self._script[self._idx]
        self._idx += 1
        return resp


def _make_registry():
    reg = ToolRegistry()
    reg.register_function("echo", "回显", lambda x: f"echo:{x}")
    reg.register_function("writer", "写操作", lambda x: f"wrote:{x}")
    return reg


def _make_agent(llm, **kwargs):
    return ReActAgent(
        name="iso", llm=llm, tool_registry=_make_registry(),
        system_prompt="默认系统提示", max_steps=kwargs.pop("max_steps", 5), **kwargs,
    )


# ------------------------------------------------------------------ P0-1 override
class TestSystemPromptOverride:
    def test_override_replaces_system_message(self):
        llm = FakeFCLLM([LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "ok"})], model="fake-fc")])
        agent = _make_agent(llm)
        agent.run_with_trace("hi", system_prompt_override="你是隔离子代理")
        assert agent.messages[0].role == "system"
        assert agent.messages[0].content == "你是隔离子代理"

    def test_override_bypasses_build_system_prompt_hook(self):
        llm = FakeFCLLM([LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "ok"})], model="fake-fc")])
        agent = _make_agent(llm)
        # 装一个会抛异常/污染的钩子，override 应绕过它。
        agent.build_system_prompt = lambda _: "钩子生成的提示"
        agent.run_with_trace("hi", system_prompt_override="覆盖提示")
        assert agent.messages[0].content == "覆盖提示"

    def test_no_override_uses_hook(self):
        llm = FakeFCLLM([LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "ok"})], model="fake-fc")])
        agent = _make_agent(llm)
        agent.build_system_prompt = lambda _: "钩子生成的提示"
        agent.run_with_trace("hi")
        assert agent.messages[0].content == "钩子生成的提示"


# ------------------------------------------------------------------ P0-1 allowed_tools
class TestAllowedTools:
    def test_schemas_filtered_by_allowed(self):
        llm = FakeFCLLM([LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "ok"})], model="fake-fc")])
        agent = _make_agent(llm)
        agent.run_with_trace("hi", allowed_tools=["echo"])
        names = {t["function"]["name"] for t in llm.last_tools}
        assert names == {"echo", "finish"}

    def test_unauthorized_tool_call_rejected(self):
        # 子代理尝试调用未授权的 writer，应拿到未授权 error 观察，然后 finish。
        llm = FakeFCLLM([
            LLMToolResponse(content=None, tool_calls=[_tc("writer", {"input": "x"})], model="fake-fc"),
            LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "done"})], model="fake-fc"),
        ])
        agent = _make_agent(llm)
        result = agent.run_with_trace("hi", allowed_tools=["echo"])
        assert result.terminated_reason == TERMINATED_FINISHED
        tool_result = result.steps[0].tool_results[0]
        assert tool_result["status"] == "error"
        assert "未授权" in tool_result["content"]

    def test_no_allowed_exposes_all(self):
        llm = FakeFCLLM([LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "ok"})], model="fake-fc")])
        agent = _make_agent(llm)
        agent.run_with_trace("hi")
        names = {t["function"]["name"] for t in llm.last_tools}
        assert {"echo", "writer", "finish"} <= names


# ------------------------------------------------------------------ P0-2 single history source
class TestHistorySingleSource:
    def test_finalize_does_not_write_base_history(self):
        llm = FakeFCLLM([LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "答案"})], model="fake-fc")])
        agent = _make_agent(llm)
        agent.run_with_trace("问题")
        # 基类 _history 不再被 _finalize 写入。
        assert agent.get_history() == []

    def test_final_answer_appended_to_messages(self):
        llm = FakeFCLLM([LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "最终答案"})], model="fake-fc")])
        agent = _make_agent(llm)
        agent.run_with_trace("问题")
        # messages 末条应为面向用户的最终答案文本。
        assert agent.messages[-1].role == "assistant"
        assert agent.messages[-1].content == "最终答案"

