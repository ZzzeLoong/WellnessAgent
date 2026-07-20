"""wellnessagent/orchestrator.py 单测（R6-5）。

用 fake LLM + fake subagent_factory 驱动，验证：
- 分诊两路径（simple / composite）+ 降级（LLM 失败 → composite）+ 强制档。
- composite pipeline：profile+retrieval 并行 → planning → safety → 聚合。
- 失败隔离：单个子代理异常不整轮崩，降级占位。
- 聚合 LLM 不可用时降级为拼接摘要。
- trace 事件写入（orchestrator_* / subagent_*）。
"""

import json

from WellnessAgent.agents.sub_agent import SubAgentResult
from WellnessAgent.wellnessagent.orchestrator import (
    Orchestrator,
    OrchestrationContext,
    ROUTE_SIMPLE,
    ROUTE_COMPOSITE,
)


class FakeLLM:
    """triage/aggregate 共用：invoke 按 script 依次返回文本。"""

    def __init__(self, script):
        self._script = list(script)
        self._idx = 0
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        if self._idx >= len(self._script):
            return ""
        text = self._script[self._idx]
        self._idx += 1
        return text


class RaisingLLM:
    def invoke(self, *a, **k):
        raise RuntimeError("llm down")


class FakeSubAgent:
    """假子代理：execute 返回预设 SubAgentResult 或抛异常。"""

    def __init__(self, name, summary="摘要", success=True, raise_exc=False):
        self.name = name
        self._summary = summary
        self._success = success
        self._raise = raise_exc
        # 供 orchestrator._run 记录 tools_allowed。
        self.tool_filter = _AllowAll()
        self.base_registry = _FakeRegistry()

    def execute(self, task, context):
        if self._raise:
            raise RuntimeError("subagent boom")
        return SubAgentResult(
            name=self.name,
            success=self._success,
            summary=self._summary,
            data={},
            steps=[],
            metadata={"steps": 1, "tools_used": [], "duration_ms": 5,
                      "terminated_reason": "finished" if self._success else "max_steps"},
        )


class _AllowAll:
    def filter(self, names):
        return list(names)


class _FakeRegistry:
    def list_tools(self):
        return ["kb_search"]


def _factory(overrides=None):
    overrides = overrides or {}

    def factory(name, parent_trace=None):
        if name in overrides:
            return overrides[name]
        return FakeSubAgent(name, summary=f"{name} 摘要")

    return factory


class RecordingTrace:
    def __init__(self):
        self.events = []

    def log_event(self, event, payload=None, step=None):
        self.events.append((event, payload or {}))


# ------------------------------------------------------------------ triage
class TestTriage:
    def test_llm_simple(self):
        llm = FakeLLM([json.dumps({"route": "simple", "reason": "闲聊"})])
        orch = Orchestrator(llm, _factory(), triage_mode="llm")
        res = orch.handle("今天天气不错", OrchestrationContext(message="今天天气不错"))
        assert res.route == ROUTE_SIMPLE
        assert res.delegate_to_monolith is True

    def test_llm_composite_runs_pipeline(self):
        # triage 返回 composite，aggregate 返回最终答案。
        llm = FakeLLM([json.dumps({"route": "composite", "reason": "复合"}), "聚合后的最终答案"])
        orch = Orchestrator(llm, _factory(), triage_mode="llm")
        res = orch.handle("7天减脂食谱避开花生", OrchestrationContext(message="7天减脂食谱避开花生"))
        assert res.route == ROUTE_COMPOSITE
        assert res.answer == "聚合后的最终答案"
        assert {r["name"] for r in res.subagent_results} == {"profile", "retrieval", "planning", "safety"}

    def test_triage_llm_failure_defaults_composite(self):
        orch = Orchestrator(RaisingLLM(), _factory(), triage_mode="llm")
        res = orch.handle("随便", OrchestrationContext(message="随便"))
        assert res.route == ROUTE_COMPOSITE

    def test_forced_simple(self):
        orch = Orchestrator(FakeLLM([]), _factory(), triage_mode="always_simple")
        res = orch.handle("7天食谱", OrchestrationContext(message="7天食谱"))
        assert res.route == ROUTE_SIMPLE

    def test_forced_composite(self):
        orch = Orchestrator(FakeLLM(["答案"]), _factory(), triage_mode="always_composite")
        res = orch.handle("你好", OrchestrationContext(message="你好"))
        assert res.route == ROUTE_COMPOSITE

    def test_rule_mode_composite_on_multi_signal(self):
        orch = Orchestrator(FakeLLM(["答案"]), _factory(), triage_mode="rule")
        res = orch.handle("帮我做7天减脂食谱", OrchestrationContext(message="帮我做7天减脂食谱"))
        assert res.route == ROUTE_COMPOSITE

    def test_rule_mode_simple_on_single_intent(self):
        orch = Orchestrator(FakeLLM([]), _factory(), triage_mode="rule")
        res = orch.handle("鸡胸肉热量多少", OrchestrationContext(message="鸡胸肉热量多少"))
        assert res.route == ROUTE_SIMPLE


# ------------------------------------------------------------------ pipeline
class TestPipeline:
    def test_failure_isolation(self):
        # planning 子代理抛异常 → 降级占位，pipeline 不崩，仍聚合。
        overrides = {"planning": FakeSubAgent("planning", raise_exc=True)}
        llm = FakeLLM([json.dumps({"route": "composite", "reason": "x"}), "最终答案"])
        orch = Orchestrator(llm, _factory(overrides), triage_mode="llm")
        res = orch.handle("复合", OrchestrationContext(message="复合"))
        assert res.route == ROUTE_COMPOSITE
        planning_brief = next(r for r in res.subagent_results if r["name"] == "planning")
        assert planning_brief["success"] is False

    def test_aggregate_fallback_when_no_llm_for_aggregate(self):
        # triage 用 always_composite 避免 triage LLM；aggregate LLM 抛异常 → 降级拼接。
        orch = Orchestrator(RaisingLLM(), _factory(), triage_mode="always_composite")
        res = orch.handle("复合", OrchestrationContext(message="复合"))
        # 降级拼接应包含 planning 摘要。
        assert "planning 摘要" in res.answer

    def test_parallel_results_present(self):
        llm = FakeLLM([json.dumps({"route": "composite", "reason": "x"}), "答案"])
        orch = Orchestrator(llm, _factory(), triage_mode="llm", parallelism=2)
        res = orch.handle("复合", OrchestrationContext(message="复合"))
        names = [r["name"] for r in res.subagent_results]
        assert "profile" in names and "retrieval" in names


# ------------------------------------------------------------------ trace
class TestTrace:
    def test_triage_and_subagent_events_logged(self):
        trace = RecordingTrace()
        llm = FakeLLM([json.dumps({"route": "composite", "reason": "x"}), "答案"])
        orch = Orchestrator(llm, _factory(), trace_logger=trace, triage_mode="llm")
        orch.handle("复合", OrchestrationContext(message="复合"))
        event_types = {e for e, _ in trace.events}
        assert "orchestrator_triage" in event_types
        assert "orchestrator_dispatch" in event_types
        assert "subagent_start" in event_types
        assert "subagent_result" in event_types
        assert "orchestrator_aggregate" in event_types

