"""WellnessPlanningAgent 编排路由单测（R6-6）。

不实例化重依赖（memory/RAG 后端），用 ``__new__`` + 桩装配，聚焦验证
``chat_with_trace`` 的 simple/composite 路由与对外契约：
- simple → 走一期单体 run_with_trace（原路径），orchestration.route == simple。
- composite → 走编排 pipeline，answer 来自聚合，经 _finalize（guardrails）落 messages。
- 对外仍含一期字段 + 附加 orchestration。
"""

from WellnessAgent.wellnessagent.agent import WellnessPlanningAgent
from WellnessAgent.wellnessagent.orchestrator import OrchestrationResult, ROUTE_SIMPLE, ROUTE_COMPOSITE
from WellnessAgent.agents.react_agent import ReActRunResult, TERMINATED_FINISHED


class _FakeOrchestrator:
    def __init__(self, result):
        self._result = result
        self.trace_logger = None
        self.handled_with = None

    def handle(self, message, ctx, resume=None):
        self.handled_with = (message, ctx, resume)
        return self._result


def _make_bare_agent(orchestration_enabled=True):
    """构造一个不触发 __init__ 重依赖的 agent 实例，仅挂路由所需属性。"""
    agent = WellnessPlanningAgent.__new__(WellnessPlanningAgent)
    agent.name = "test"
    agent.user_id = "u1"
    agent.messages = []
    agent.session_id = "s-test"
    agent._orchestration_enabled = orchestration_enabled
    agent._hitl_enabled = True
    agent._pending_confirmation = None
    agent.trace_logger = None
    agent.guardrails = None
    agent.get_guardrail_profile = lambda: None
    agent.build_system_prompt = None
    agent.system_prompt = "系统提示"
    agent.fallback_prompt_suffix = ""

    class _LLM:
        def supports_function_calling(self):
            return True

    agent.llm = _LLM()

    # 桩：收尾相关方法都 no-op，避免触碰真实后端。
    agent._ensure_session = lambda: None
    agent._handle_post_turn_memory = lambda: None
    agent._persist_session = lambda pending=None: None
    agent.get_state_dict = lambda: {"stub": True}
    agent._build_orchestration_context = lambda user_input: object()

    # history_manager 桩（_finalize / _append_message 用到）。
    class _HM:
        def append(self, m):
            pass

        def set_history(self, m):
            pass

    agent.history_manager = _HM()
    return agent


class TestSimpleRoute:
    def test_simple_delegates_to_monolith(self):
        agent = _make_bare_agent()
        agent.orchestrator = _FakeOrchestrator(
            OrchestrationResult(route=ROUTE_SIMPLE, delegate_to_monolith=True)
        )
        # 单体路径桩：run_with_trace 返回固定结果。
        called = {}

        def fake_run_with_trace(user_input, **kwargs):
            called["ran"] = user_input
            return ReActRunResult(final_answer="单体答案", steps=[], terminated_reason=TERMINATED_FINISHED)

        agent.run_with_trace = fake_run_with_trace

        resp = agent.chat_with_trace("你好")
        assert called["ran"] == "你好"
        assert resp["answer"] == "单体答案"
        assert resp["orchestration"]["route"] == ROUTE_SIMPLE


class TestCompositeRoute:
    def test_composite_uses_aggregated_answer(self):
        agent = _make_bare_agent()
        agent.orchestrator = _FakeOrchestrator(
            OrchestrationResult(
                route=ROUTE_COMPOSITE,
                answer="编排聚合答案",
                subagent_results=[{"name": "planning", "success": True}],
            )
        )
        # composite 不应调用单体 run_with_trace。
        def boom(*a, **k):
            raise AssertionError("composite 不应走单体 run_with_trace")

        agent.run_with_trace = boom

        resp = agent.chat_with_trace("给我7天减脂食谱，避开花生，预算有限")
        assert resp["answer"] == "编排聚合答案"
        assert resp["orchestration"]["route"] == ROUTE_COMPOSITE
        # 聚合答案应被 _finalize 追加进 messages（含 user + assistant）。
        roles = [m.role for m in agent.messages]
        assert "user" in roles and "assistant" in roles
        assert agent.messages[-1].content == "编排聚合答案"

    def test_composite_runs_guardrails_finalize(self):
        agent = _make_bare_agent()
        agent.orchestrator = _FakeOrchestrator(
            OrchestrationResult(route=ROUTE_COMPOSITE, answer="含花生的计划")
        )

        class _Rewrite:
            def check(self, answer, profile):
                from WellnessAgent.wellnessagent.guardrails import GuardrailResult
                return GuardrailResult(action="rewrite", reason="t", hits=["花生"], safe_text=f"[安全]{answer}")

        agent.guardrails = _Rewrite()
        agent.run_with_trace = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no monolith"))

        resp = agent.chat_with_trace("复合请求")
        # guardrails rewrite 应生效。
        assert resp["answer"] == "[安全]含花生的计划"


class TestOrchestrationDisabled:
    def test_disabled_always_monolith(self):
        agent = _make_bare_agent(orchestration_enabled=False)
        # orchestrator 存在但不应被调用。
        agent.orchestrator = _FakeOrchestrator(
            OrchestrationResult(route=ROUTE_COMPOSITE, answer="不该用到")
        )
        agent.run_with_trace = lambda user_input, **k: ReActRunResult(
            final_answer="单体", steps=[], terminated_reason=TERMINATED_FINISHED
        )
        resp = agent.chat_with_trace("任意")
        assert resp["answer"] == "单体"
        assert "orchestration" not in resp
        assert agent.orchestrator.handled_with is None

