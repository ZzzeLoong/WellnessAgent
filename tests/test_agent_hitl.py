"""WellnessPlanningAgent HITL 挂起/恢复单测（R7）。

用 ``__new__`` + 桩装配，聚焦验证：
- composite 命中高影响步骤 → 挂起：返回 confirmation，terminated_reason 对外 finished，
  挂起态写入 SessionStore.metadata。
- 下一轮带 confirmation → 恢复：画像落库、清挂起态、写 confirm_resume trace。
- confirm_id 不匹配 → 忽略确认（按新请求处理）。
"""

from WellnessAgent.agents.react_agent import ReActRunResult, TERMINATED_FINISHED
from WellnessAgent.core.hitl import KIND_PROFILE_UPDATE, PendingConfirmation
from WellnessAgent.core.message import Message
from WellnessAgent.core.session_store import SessionStore
from WellnessAgent.wellnessagent.agent import WellnessPlanningAgent
from WellnessAgent.wellnessagent.orchestrator import (
    OrchestrationResult,
    ROUTE_COMPOSITE,
    ROUTE_SIMPLE,
)


class _FakeOrchestrator:
    def __init__(self, result):
        self._result = result
        self.trace_logger = None
        self.handled_with = None

    def handle(self, message, ctx, resume=None):
        self.handled_with = (message, ctx, resume)
        return self._result


class _RecordingService:
    def __init__(self):
        self.profile_set_calls = []

    def profile_set(self, updates):
        self.profile_set_calls.append(updates)
        return "ok"

    def get_current_profile(self):
        return None


class _RecordingTrace:
    def __init__(self):
        self.events = []

    def log_event(self, event, payload=None, step=None):
        self.events.append((event, payload or {}))

    def finalize(self):
        return {}


def _make_agent(tmp_path, orchestration_result):
    agent = WellnessPlanningAgent.__new__(WellnessPlanningAgent)
    agent.name = "test"
    agent.user_id = "u1"
    agent.messages = []
    agent.session_id = "s-test"
    agent._orchestration_enabled = True
    agent._hitl_enabled = True
    agent._pending_confirmation = None
    agent.trace_logger = _RecordingTrace()
    agent.guardrails = None
    agent.get_guardrail_profile = lambda: None
    agent.build_system_prompt = None
    agent.system_prompt = "系统提示"
    agent.fallback_prompt_suffix = ""

    class _LLM:
        def supports_function_calling(self):
            return True

    agent.llm = _LLM()

    class _Registry:
        def get_tools_description(self):
            return ""

    agent.tool_registry = _Registry()
    agent.service = _RecordingService()
    agent.session_store = SessionStore(session_dir=str(tmp_path / "sessions"))

    agent._ensure_session = lambda: None
    agent._handle_post_turn_memory = lambda: None
    agent.get_state_dict = lambda: {"stub": True}
    agent._build_orchestration_context = lambda user_input: object()
    agent.orchestrator = _FakeOrchestrator(orchestration_result)

    class _HM:
        def append(self, m):
            pass

        def set_history(self, m):
            pass

    agent.history_manager = _HM()
    return agent


def _pending(confirm_id="c-1"):
    return PendingConfirmation(
        confirm_id=confirm_id,
        kind=KIND_PROFILE_UPDATE,
        prompt="确认写入 allergies？",
        payload={"suggested_updates": {"allergies": ["花生"]}},
    )


class TestSuspend:
    def test_composite_pending_returns_confirmation(self, tmp_path):
        pending = _pending()
        result = OrchestrationResult(
            route=ROUTE_COMPOSITE, answer=pending.prompt,
            subagent_results=[{"name": "profile", "success": True}], pending=pending,
        )
        agent = _make_agent(tmp_path, result)

        resp = agent.chat_with_trace("给我7天减脂食谱")
        # 对外 terminated_reason 映射为 finished。
        assert resp["terminated_reason"] == TERMINATED_FINISHED
        assert resp["confirmation"]["confirm_id"] == "c-1"
        # 挂起态写入 SessionStore.metadata。
        saved = agent.session_store.load("u1", "s-test")
        assert saved["metadata"]["pending_confirmation"]["confirm_id"] == "c-1"
        # confirm_request trace 事件由 orchestrator 记（此处 fake），但挂起后运行时缓存已设。
        assert agent._pending_confirmation is not None


class TestResume:
    def test_approve_persists_profile_and_clears_pending(self, tmp_path):
        # 首轮挂起。
        pending = _pending()
        suspend_result = OrchestrationResult(
            route=ROUTE_COMPOSITE, answer=pending.prompt, pending=pending,
        )
        agent = _make_agent(tmp_path, suspend_result)
        agent.chat_with_trace("给我7天减脂食谱")

        # 下一轮：orchestrator 返回聚合答案（无 pending），带 confirmation 恢复。
        agent.orchestrator = _FakeOrchestrator(
            OrchestrationResult(route=ROUTE_COMPOSITE, answer="最终计划")
        )
        resp = agent.chat_with_trace(
            "（已确认）",
            confirmation={"confirm_id": "c-1", "decision": "approve"},
        )
        assert resp["answer"] == "最终计划"
        # 画像被落库（敏感字段 allergies）。
        assert agent.service.profile_set_calls
        assert agent.service.profile_set_calls[0].get("allergies") == ["花生"]
        # resume 上下文被传给 orchestrator。
        _msg, _ctx, resume = agent.orchestrator.handled_with
        assert resume is not None and resume["decision"] == "approve"
        # 挂起态清除。
        assert agent._pending_confirmation is None
        # confirm_resume trace 事件已写。
        assert any(e == "confirm_resume" for e, _ in agent.trace_logger.events)

    def test_reject_does_not_persist_profile(self, tmp_path):
        pending = _pending()
        agent = _make_agent(
            tmp_path,
            OrchestrationResult(route=ROUTE_COMPOSITE, answer=pending.prompt, pending=pending),
        )
        agent.chat_with_trace("请求")
        agent.orchestrator = _FakeOrchestrator(
            OrchestrationResult(route=ROUTE_COMPOSITE, answer="按原约束的计划")
        )
        agent.chat_with_trace(
            "（已拒绝）", confirmation={"confirm_id": "c-1", "decision": "reject"}
        )
        assert agent.service.profile_set_calls == []

    def test_mismatched_confirm_id_ignored(self, tmp_path):
        pending = _pending("c-1")
        agent = _make_agent(
            tmp_path,
            OrchestrationResult(route=ROUTE_COMPOSITE, answer=pending.prompt, pending=pending),
        )
        agent.chat_with_trace("请求")
        # 用错误 confirm_id 恢复 → resume 应为 None（按新请求处理，不落库）。
        agent.orchestrator = _FakeOrchestrator(
            OrchestrationResult(route=ROUTE_COMPOSITE, answer="新答案")
        )
        agent.chat_with_trace(
            "确认", confirmation={"confirm_id": "c-WRONG", "decision": "approve"}
        )
        _msg, _ctx, resume = agent.orchestrator.handled_with
        assert resume is None
        assert agent.service.profile_set_calls == []

