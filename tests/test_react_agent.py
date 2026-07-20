"""agents/react_agent.py 单元测试（离线，用 fake LLM 驱动）。

覆盖：
- FC 路径：finish 终止、单工具、多工具聚合、max_steps、异常兜底、空响应。
- 回退路径（json_fallback）：JSON 解析 finish / tool、无法解析兜底。
- Guardrails 终止前钩子（rewrite）。
- 上下文压缩触发（低阈值强制）。
- _parse_arguments / StepRecord 结构。
"""

import json
import uuid

import pytest

from WellnessAgent.agents.react_agent import (
    ReActAgent,
    ReActRunResult,
    StepRecord,
    TERMINATED_FINISHED,
    TERMINATED_MAX_STEPS,
    TERMINATED_LLM_EMPTY,
)
from WellnessAgent.core.llm_response import LLMToolResponse, ToolCall
from WellnessAgent.tools.registry import ToolRegistry, FINISH_TOOL_NAME


# ==================================================================
# Fakes
# ==================================================================
def _tc(name: str, arguments: dict, call_id: str | None = None) -> ToolCall:
    return ToolCall(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        name=name,
        arguments=json.dumps(arguments, ensure_ascii=False),
    )


class FakeFCLLM:
    """FC 路径 fake LLM：按预设脚本依次返回 LLMToolResponse。"""

    def __init__(self, script: list[LLMToolResponse], model: str = "fake-fc"):
        self.model = model
        self.provider = "openai"
        self._script = script
        self._idx = 0
        self.calls: list = []

    def supports_function_calling(self) -> bool:
        return True

    def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs):
        self.calls.append(messages)
        if self._idx >= len(self._script):
            # 脚本耗尽：返回空响应（不再调用工具）。
            return LLMToolResponse(content="", tool_calls=[], model=self.model)
        resp = self._script[self._idx]
        self._idx += 1
        return resp

    def invoke(self, messages, **kwargs):  # 不应被 FC 路径调用
        raise AssertionError("FC path should not call invoke()")


class FakeFallbackLLM:
    """回退路径 fake LLM：supports_function_calling=False，invoke 返回预设文本。"""

    def __init__(self, script: list[str], model: str = "fake-fallback"):
        self.model = model
        self.provider = "ollama"
        self._script = script
        self._idx = 0
        self.invoke_calls = 0

    def supports_function_calling(self) -> bool:
        return False

    def invoke(self, messages, **kwargs):
        self.invoke_calls += 1
        if self._idx >= len(self._script):
            return ""
        text = self._script[self._idx]
        self._idx += 1
        return text

    def invoke_with_tools(self, *a, **k):  # 不应被回退路径调用
        raise AssertionError("fallback path should not call invoke_with_tools()")


class RaisingLLM:
    """FC 调用抛异常。"""

    model = "raising"
    provider = "openai"

    def supports_function_calling(self) -> bool:
        return True

    def invoke_with_tools(self, *a, **k):
        raise RuntimeError("boom")


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_function("echo", "回显输入", lambda x: f"echo:{x}")
    reg.register_function("adder", "把 a 和 b 相加", lambda a, b: str(int(a) + int(b)),
                          parameters=[
                              {"name": "a", "type": "string", "required": True},
                              {"name": "b", "type": "string", "required": True},
                          ])
    return reg


def make_agent(llm, registry=None, **kwargs) -> ReActAgent:
    agent = ReActAgent(
        name="tester",
        llm=llm,
        tool_registry=registry or make_registry(),
        system_prompt="你是测试助手。",
        max_steps=kwargs.pop("max_steps", 5),
        **kwargs,
    )
    return agent


# ==================================================================
# FC 路径
# ==================================================================
class TestFCPath:
    def test_direct_finish(self):
        llm = FakeFCLLM([
            LLMToolResponse(content="thinking", tool_calls=[_tc("finish", {"answer": "最终答案"})], model="fake-fc"),
        ])
        agent = make_agent(llm)
        result = agent.run_with_trace("你好")
        assert isinstance(result, ReActRunResult)
        assert result.final_answer == "最终答案"
        assert result.terminated_reason == TERMINATED_FINISHED

    def test_single_tool_then_finish(self):
        llm = FakeFCLLM([
            LLMToolResponse(content=None, tool_calls=[_tc("echo", {"input": "hi"})], model="fake-fc"),
            LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "done"})], model="fake-fc"),
        ])
        agent = make_agent(llm)
        result = agent.run_with_trace("call echo")
        assert result.final_answer == "done"
        assert result.terminated_reason == TERMINATED_FINISHED
        # 第一步是工具调用，应聚合出 tool_results。
        tool_step = result.steps[0]
        assert tool_step.tool_calls[0]["name"] == "echo"
        assert tool_step.tool_results[0]["content"] == "echo:hi"
        assert tool_step.tool_results[0]["status"] == "success"

    def test_multi_tool_in_one_step(self):
        """一步发起多个工具调用应聚合进同一个 StepRecord。"""
        llm = FakeFCLLM([
            LLMToolResponse(
                content=None,
                tool_calls=[_tc("echo", {"input": "a"}), _tc("echo", {"input": "b"})],
                model="fake-fc",
            ),
            LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "ok"})], model="fake-fc"),
        ])
        agent = make_agent(llm)
        result = agent.run_with_trace("multi")
        step = result.steps[0]
        assert len(step.tool_calls) == 2
        assert len(step.tool_results) == 2
        assert step.tool_results[0]["content"] == "echo:a"
        assert step.tool_results[1]["content"] == "echo:b"

    def test_structured_tool_arguments(self):
        """结构化多参数工具应按 dict 分发。"""
        llm = FakeFCLLM([
            LLMToolResponse(content=None, tool_calls=[_tc("adder", {"a": "2", "b": "3"})], model="fake-fc"),
            LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "5"})], model="fake-fc"),
        ])
        agent = make_agent(llm)
        result = agent.run_with_trace("2+3")
        assert result.steps[0].tool_results[0]["content"] == "5"

    def test_max_steps(self):
        """一直调用工具不 finish → max_steps 终止。"""
        script = [
            LLMToolResponse(content=None, tool_calls=[_tc("echo", {"input": str(i)})], model="fake-fc")
            for i in range(10)
        ]
        llm = FakeFCLLM(script)
        agent = make_agent(llm, max_steps=3)
        result = agent.run_with_trace("loop")
        assert result.terminated_reason == TERMINATED_MAX_STEPS
        assert len(result.steps) == 3

    def test_llm_exception_finalizes_gracefully(self):
        agent = make_agent(RaisingLLM())
        result = agent.run_with_trace("boom")
        assert result.terminated_reason == TERMINATED_LLM_EMPTY
        assert "异常" in result.final_answer

    def test_empty_response_no_prior_steps(self):
        llm = FakeFCLLM([
            LLMToolResponse(content="", tool_calls=[], model="fake-fc"),
        ])
        agent = make_agent(llm)
        result = agent.run_with_trace("empty")
        assert result.terminated_reason == TERMINATED_LLM_EMPTY

    def test_plain_text_answer_without_tool_calls(self):
        """无 tool_calls 但有 content → 视为最终答案 finished。"""
        llm = FakeFCLLM([
            LLMToolResponse(content="直接回答", tool_calls=[], model="fake-fc"),
        ])
        agent = make_agent(llm)
        result = agent.run_with_trace("hi")
        assert result.final_answer == "直接回答"
        assert result.terminated_reason == TERMINATED_FINISHED

    def test_messages_accumulate_with_system(self):
        """messages 首条应为 system，且累积 user/assistant/tool。"""
        llm = FakeFCLLM([
            LLMToolResponse(content=None, tool_calls=[_tc("echo", {"input": "x"})], model="fake-fc"),
            LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "end"})], model="fake-fc"),
        ])
        agent = make_agent(llm)
        agent.run_with_trace("hello")
        roles = [m.role for m in agent.messages]
        assert roles[0] == "system"
        assert "user" in roles
        assert "assistant" in roles
        assert "tool" in roles


# ==================================================================
# 回退路径
# ==================================================================
class TestFallbackPath:
    def test_json_finish(self):
        llm = FakeFallbackLLM([
            json.dumps({"thought": "done thinking", "action": {"type": "finish", "answer": "回退答案"}}),
        ])
        agent = make_agent(llm)
        result = agent.run_with_trace("q")
        assert result.final_answer == "回退答案"
        assert result.terminated_reason == TERMINATED_FINISHED
        assert result.steps[0].source == "json_fallback"

    def test_json_tool_then_finish(self):
        llm = FakeFallbackLLM([
            json.dumps({"thought": "use echo", "action": {"type": "tool", "name": "echo", "input": "hey"}}),
            json.dumps({"thought": "", "action": {"type": "finish", "answer": "final"}}),
        ])
        agent = make_agent(llm)
        result = agent.run_with_trace("q")
        assert result.final_answer == "final"
        assert result.steps[0].tool_results[0]["content"] == "echo:hey"

    def test_unparseable_text_is_final_answer(self):
        """无法解析成动作 → 文本兜底为最终答案。"""
        llm = FakeFallbackLLM(["这是一段没有 JSON 的普通文本"])
        agent = make_agent(llm)
        result = agent.run_with_trace("q")
        assert result.final_answer == "这是一段没有 JSON 的普通文本"
        assert result.terminated_reason == TERMINATED_FINISHED


# ==================================================================
# Guardrails 钩子
# ==================================================================
class _RewriteGuardrails:
    """总是 rewrite 的假 guardrails。"""

    def check(self, answer, profile):
        from WellnessAgent.wellnessagent.guardrails import GuardrailResult
        return GuardrailResult(action="rewrite", reason="test", hits=["x"], safe_text=f"[安全]{answer}")


class TestGuardrailsHook:
    def test_rewrite_applied_on_finish(self):
        llm = FakeFCLLM([
            LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "原始答案"})], model="fake-fc"),
        ])
        agent = make_agent(llm)
        agent.guardrails = _RewriteGuardrails()
        agent.get_guardrail_profile = lambda: None
        result = agent.run_with_trace("q")
        assert result.final_answer == "[安全]原始答案"

    def test_guardrails_not_applied_on_max_steps(self):
        """非 finished 终止不应触发 guardrails 改写。"""
        script = [
            LLMToolResponse(content=None, tool_calls=[_tc("echo", {"input": str(i)})], model="fake-fc")
            for i in range(10)
        ]
        agent = make_agent(FakeFCLLM(script), max_steps=2)
        agent.guardrails = _RewriteGuardrails()
        agent.get_guardrail_profile = lambda: None
        result = agent.run_with_trace("q")
        assert not result.final_answer.startswith("[安全]")


# ==================================================================
# _parse_arguments
# ==================================================================
class TestParseArguments:
    def test_single_input_degrades_to_string(self):
        agent = make_agent(FakeFCLLM([]))
        parsed = agent._parse_arguments(_tc("echo", {"input": "hello"}))
        assert parsed == "hello"

    def test_multi_field_stays_dict(self):
        agent = make_agent(FakeFCLLM([]))
        parsed = agent._parse_arguments(_tc("adder", {"a": "1", "b": "2"}))
        assert parsed == {"a": "1", "b": "2"}

    def test_empty_arguments(self):
        agent = make_agent(FakeFCLLM([]))
        parsed = agent._parse_arguments(ToolCall(id="c1", name="echo", arguments=""))
        assert parsed == ""

    def test_invalid_json_returns_raw(self):
        agent = make_agent(FakeFCLLM([]))
        parsed = agent._parse_arguments(ToolCall(id="c1", name="echo", arguments="not json"))
        assert parsed == "not json"


# ==================================================================
# 上下文压缩
# ==================================================================
class TestCompression:
    def test_compress_triggered_with_low_window(self):
        """把 context_window 设得极小，强制触发压缩且不崩。"""
        llm = FakeFCLLM([
            LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "ok"})], model="fake-fc"),
        ])
        agent = make_agent(llm, context_window=1)  # 阈值极低必然触发
        # 预置一些历史轮次
        agent.history_manager.min_retain_rounds = 1
        result = agent.run_with_trace("hello")
        assert result.terminated_reason == TERMINATED_FINISHED


# ==================================================================
# StepRecord
# ==================================================================
class TestStreamRun:
    def test_stream_run_event_sequence(self):
        """stream_run 应逐步产出事件，末尾产出 result 事件。"""
        llm = FakeFCLLM([
            LLMToolResponse(content="思考", tool_calls=[_tc("echo", {"input": "hi"})], model="fake-fc"),
            LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "done"})], model="fake-fc"),
        ])
        agent = make_agent(llm)
        events = list(agent.stream_run("call echo"))
        types = [e["type"] for e in events]
        # 首个 step 前有 step_start，工具步有 tool_call/tool_result，最终 result。
        assert types[0] == "step_start"
        assert "tool_call" in types
        assert "tool_result" in types
        assert types[-1] == "result"
        result = events[-1]["result"]
        assert result.final_answer == "done"
        assert result.terminated_reason == TERMINATED_FINISHED

    def test_stream_run_matches_run_with_trace_final(self):
        """流式与非流式对同一脚本应得到一致的最终答案。"""
        script = [
            LLMToolResponse(content=None, tool_calls=[_tc("finish", {"answer": "同一答案"})], model="fake-fc"),
        ]
        agent_stream = make_agent(FakeFCLLM(list(script)))
        agent_plain = make_agent(FakeFCLLM(list(script)))
        stream_events = list(agent_stream.stream_run("hi"))
        stream_answer = stream_events[-1]["result"].final_answer
        plain_answer = agent_plain.run_with_trace("hi").final_answer
        assert stream_answer == plain_answer == "同一答案"


class TestStepRecord:
    def test_to_dict_contains_new_structure_fields(self):
        step = StepRecord(
            index=1,
            role="assistant",
            thought="t",
            tool_calls=[{"id": "c1", "name": "echo", "arguments": "{}"}],
            tool_results=[{"tool_call_id": "c1", "name": "echo", "status": "success", "content": "r"}],
            source="function_calling",
        )
        d = step.to_dict()
        # 新结构字段
        assert d["index"] == 1
        assert d["role"] == "assistant"
        assert d["tool_calls"][0]["name"] == "echo"
        assert d["tool_results"][0]["status"] == "success"
        assert d["source"] == "function_calling"
        # 旧别名字段已移除
        assert "step_index" not in d
        assert "tool_name" not in d
        assert "tool_input" not in d
        assert "observation" not in d
        assert "action_text" not in d

