"""orchestrator.py HITL 关卡单测（R7）：命中挂起 / resume 恢复 / 开关关闭。"""

from WellnessAgent.agents.sub_agent import SubAgentResult
from WellnessAgent.core.hitl import KIND_PROFILE_UPDATE, KIND_SAFETY_RISK
from WellnessAgent.wellnessagent.orchestrator import (
    Orchestrator,
    OrchestrationContext,
    ROUTE_COMPOSITE,
)


class FakeLLM:
    def __init__(self, script):
        self._script = list(script)
        self._idx = 0

    def invoke(self, messages, **kwargs):
        if self._idx >= len(self._script):
            return "聚合答案"
        text = self._script[self._idx]
        self._idx += 1
        return text


class FakeSubAgent:
    """可携带结构化 data 的假子代理。"""

    def __init__(self, name, data=None, success=True):
        self.name = name
        self._data = data or {}
        self._success = success
        self.tool_filter = _AllowAll()
        self.base_registry = _FakeRegistry()

    def execute(self, task, context):
        return SubAgentResult(
            name=self.name,
            success=self._success,
            summary=f"{self.name} 摘要",
            data=self._data,
            steps=[],
            metadata={"steps": 1, "tools_used": [], "duration_ms": 3,
                      "terminated_reason": "finished"},
        )


class _AllowAll:
    def filter(self, names):
        return list(names)


class _FakeRegistry:
    def list_tools(self):
        return ["kb_search"]


def _factory(overrides):
    def factory(name, parent_trace=None):
        if name in overrides:
            return overrides[name]
        return FakeSubAgent(name)
    return factory


class TestHitlGate:
    def test_safety_risk_triggers_pending(self):
        overrides = {
            "safety": FakeSubAgent("safety", data={"risk": True, "hits": ["花生"],
                                                   "advice": "换成南瓜子"}),
        }
        orch = Orchestrator(FakeLLM([]), _factory(overrides),
                            triage_mode="always_composite", hitl_enabled=True)
        res = orch.handle("复合", OrchestrationContext(message="复合", risk_terms=["花生"]))
        assert res.route == ROUTE_COMPOSITE
        assert res.pending is not None
        assert res.pending.kind == KIND_SAFETY_RISK
        assert "花生" in res.pending.payload["hits"]

    def test_profile_sensitive_triggers_pending(self):
        overrides = {
            "profile": FakeSubAgent("profile", data={
                "suggested_updates": {"allergies": ["虾"], "preferred_cuisines": ["中式"]},
            }),
        }
        orch = Orchestrator(FakeLLM([]), _factory(overrides),
                            triage_mode="always_composite", hitl_enabled=True)
        res = orch.handle("复合", OrchestrationContext(message="复合"))
        assert res.pending is not None
        assert res.pending.kind == KIND_PROFILE_UPDATE
        # 只保留敏感字段 allergies，非敏感 preferred_cuisines 不触发。
        assert "allergies" in res.pending.payload["suggested_updates"]
        assert "preferred_cuisines" not in res.pending.payload["suggested_updates"]

    def test_non_sensitive_profile_no_pending(self):
        overrides = {
            "profile": FakeSubAgent("profile", data={
                "suggested_updates": {"preferred_cuisines": ["中式"]},
            }),
        }
        orch = Orchestrator(FakeLLM(["聚合"]), _factory(overrides),
                            triage_mode="always_composite", hitl_enabled=True)
        res = orch.handle("复合", OrchestrationContext(message="复合"))
        assert res.pending is None
        assert res.answer == "聚合"

    def test_hitl_disabled_no_pending(self):
        overrides = {
            "safety": FakeSubAgent("safety", data={"risk": True, "hits": ["花生"]}),
        }
        orch = Orchestrator(FakeLLM(["聚合"]), _factory(overrides),
                            triage_mode="always_composite", hitl_enabled=False)
        res = orch.handle("复合", OrchestrationContext(message="复合", risk_terms=["花生"]))
        assert res.pending is None
        assert res.answer == "聚合"

    def test_resume_skips_pending_and_aggregates(self):
        overrides = {
            "safety": FakeSubAgent("safety", data={"risk": True, "hits": ["花生"]}),
        }
        orch = Orchestrator(FakeLLM(["最终聚合答案"]), _factory(overrides),
                            triage_mode="always_composite", hitl_enabled=True)
        resume = {"decision": "approve", "kind": KIND_SAFETY_RISK,
                  "applied": {"accepted": "safe_alternative"}}
        res = orch.handle("复合", OrchestrationContext(message="复合", risk_terms=["花生"]),
                          resume=resume)
        # resume 存在 → 跳过挂起，直接聚合。
        assert res.pending is None
        assert res.answer == "最终聚合答案"

