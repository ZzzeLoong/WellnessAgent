"""WellnessPlanningAgent 建议闭环端到端单测（三期 R11 批次 3，方案 §3.3–3.5）。

沿用 ``test_agent_vision_turn.py`` 的 ``__new__`` + 桩装配范式，不触碰真实
memory/RAG/LLM 后端，聚焦验证**接缝是否焊上**：

composite 路由 → ``_maybe_record_advice`` → ``advice_store``
  → ``_build_system_prompt`` 注入待回访段
  → ``advice_feedback`` 工具 → 状态机 + episodic 记忆

**最重要的一组断言是「没有待回访建议时 system 消息与二期字节级等价」**——
这是 R11 不破坏一/二期资产的硬保证。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from WellnessAgent.wellnessagent.advice_store import (
    STATUS_ADOPTED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    AdviceRecord,
    AdviceStore,
)
from WellnessAgent.wellnessagent.agent import WellnessPlanningAgent, _humanize_age
from WellnessAgent.wellnessagent.orchestrator import OrchestrationResult, ROUTE_COMPOSITE


# ------------------------------------------------------------------ 桩
class _RecordingMemoryManager:
    def __init__(self):
        self.added: list[dict] = []

    def add_memory(self, content, memory_type, importance, metadata, auto_classify):
        self.added.append(
            {
                "content": content,
                "memory_type": memory_type,
                "importance": importance,
                "metadata": metadata,
            }
        )
        return "m-1"


class _StubMemoryTool:
    def __init__(self):
        self.memory_manager = _RecordingMemoryManager()


class _StubService:
    def __init__(self):
        self.memory_tool = _StubMemoryTool()

    def get_current_profile_summary(self):
        return "allergies: 花生"

    def get_distilled_memory_summary(self, limit=4):
        return "（无）"

    def get_working_context_summary(self, limit=6, max_chars=900):
        return "（无）"


class _TraceRecorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def log_event(self, event, payload=None, step=None):
        self.events.append((event, payload or {}))

    def finalize(self):
        pass

    def names(self):
        return [e for e, _ in self.events]

    def payload_for(self, event):
        return next((p for e, p in self.events if e == event), None)


def _agent(tmp_path, user_id="u1", with_trace=True):
    """装配一个不触发重依赖的 agent，advice_store 指向 tmp_path。"""
    agent = WellnessPlanningAgent.__new__(WellnessPlanningAgent)
    agent.user_id = user_id
    agent.name = "test"
    agent.session_id = "s-1"
    agent.tool_groups = frozenset({"memory"})
    agent.service = _StubService()
    agent.advice_store = AdviceStore(advice_dir=str(tmp_path / "advice"))
    agent.trace_logger = _TraceRecorder() if with_trace else None
    agent._last_vision = None
    return agent


def _orch(planning_success=True, planning_data=None, summary="这是一份7天减脂计划。"):
    return OrchestrationResult(
        route=ROUTE_COMPOSITE,
        answer="最终答案",
        subagent_results=[
            {"name": "profile", "success": True, "summary": "画像"},
            {"name": "planning", "success": planning_success, "summary": summary},
            {"name": "safety", "success": True, "summary": "安全"},
        ],
        planning_data=planning_data or {},
    )


def _due_record(agent, advice_id="a-1", summary="7天减脂食谱", days_ago=3):
    created = datetime.now() - timedelta(days=days_ago)
    agent.advice_store.append(
        AdviceRecord(
            advice_id=advice_id,
            user_id=agent.user_id,
            summary=summary,
            created_at=created.isoformat(),
            followup_due_at=(created + timedelta(hours=24)).isoformat(),
        )
    )


# ==================================================================
# 建议记录时机（方案 §3.3）
# ==================================================================
class TestAdviceRecording:
    def test_records_on_successful_planning(self, tmp_path):
        agent = _agent(tmp_path)
        advice_id = agent._maybe_record_advice(_orch(), "最终答案")
        assert advice_id
        assert len(agent.advice_store.list("u1")) == 1

    def test_skips_when_planning_failed(self, tmp_path):
        agent = _agent(tmp_path)
        assert agent._maybe_record_advice(_orch(planning_success=False), "答案") is None
        assert agent.advice_store.list("u1") == []

    def test_skips_when_no_planning_subagent(self, tmp_path):
        agent = _agent(tmp_path)
        orch = OrchestrationResult(
            route=ROUTE_COMPOSITE,
            answer="a",
            subagent_results=[{"name": "profile", "success": True, "summary": "x"}],
        )
        assert agent._maybe_record_advice(orch, "答案") is None

    def test_skips_in_push_mode(self, tmp_path):
        agent = _agent(tmp_path)
        agent._push_mode = True
        # 推送本身是系统发起的，再记成「待用户回访」会让队列自我繁殖
        assert agent._maybe_record_advice(_orch(), "答案") is None

    def test_recorded_status_is_pending(self, tmp_path):
        agent = _agent(tmp_path)
        advice_id = agent._maybe_record_advice(_orch(), "答案")
        assert agent.advice_store.get("u1", advice_id).status == STATUS_PENDING

    def test_session_id_recorded(self, tmp_path):
        agent = _agent(tmp_path)
        advice_id = agent._maybe_record_advice(_orch(), "答案")
        assert agent.advice_store.get("u1", advice_id).session_id == "s-1"

    def test_trace_event_emitted(self, tmp_path):
        agent = _agent(tmp_path)
        agent._maybe_record_advice(_orch(), "答案")
        assert "advice_recorded" in agent.trace_logger.names()

    def test_trace_payload_has_preview(self, tmp_path):
        agent = _agent(tmp_path)
        agent._maybe_record_advice(_orch(), "答案")
        payload = agent.trace_logger.payload_for("advice_recorded")
        assert payload["advice_id"]
        assert "summary_preview" in payload

    def test_works_without_trace_logger(self, tmp_path):
        agent = _agent(tmp_path, with_trace=False)
        assert agent._maybe_record_advice(_orch(), "答案")

    def test_store_failure_does_not_raise(self, tmp_path):
        agent = _agent(tmp_path)

        class _BoomStore:
            def append(self, record):
                raise RuntimeError("disk full")

        agent.advice_store = _BoomStore()
        # 建议记录是旁路副作用，绝不能拖垮已经生成好的回答
        assert agent._maybe_record_advice(_orch(), "答案") is None


# ==================================================================
# plan_digest 抽取与摘要回落（方案 §3.2，评审选定 C 方案）
# ==================================================================
class TestPlanDigest:
    def test_headline_used_as_summary(self, tmp_path):
        agent = _agent(tmp_path)
        orch = _orch(planning_data={"headline": "7天减脂午餐备餐"})
        advice_id = agent._maybe_record_advice(orch, "答案")
        assert agent.advice_store.get("u1", advice_id).summary == "7天减脂午餐备餐"

    def test_digest_fields_extracted(self, tmp_path):
        agent = _agent(tmp_path)
        orch = _orch(
            planning_data={
                "headline": "H",
                "days": 7,
                "meals": ["早餐", "午餐"],
                "constraints_used": ["无花生"],
                "key_items": ["鸡胸"],
            }
        )
        digest = agent.advice_store.get(
            "u1", agent._maybe_record_advice(orch, "答案")
        ).plan_digest
        assert digest["days"] == 7
        assert digest["meals"] == ["早餐", "午餐"]
        assert digest["constraints_used"] == ["无花生"]

    def test_float_days_coerced_to_int(self, tmp_path):
        agent = _agent(tmp_path)
        orch = _orch(planning_data={"headline": "H", "days": 7.0})
        digest = agent._extract_plan_digest(orch)
        assert digest["days"] == 7

    def test_bool_days_rejected(self, tmp_path):
        agent = _agent(tmp_path)
        assert "days" not in agent._extract_plan_digest(
            _orch(planning_data={"headline": "H", "days": True})
        )

    def test_null_days_omitted(self, tmp_path):
        agent = _agent(tmp_path)
        assert "days" not in agent._extract_plan_digest(
            _orch(planning_data={"headline": "H", "days": None})
        )

    def test_empty_lists_omitted(self, tmp_path):
        agent = _agent(tmp_path)
        digest = agent._extract_plan_digest(
            _orch(planning_data={"headline": "H", "meals": []})
        )
        assert "meals" not in digest

    def test_lists_capped(self, tmp_path):
        agent = _agent(tmp_path)
        digest = agent._extract_plan_digest(
            _orch(planning_data={"headline": "H", "key_items": [f"i{i}" for i in range(20)]})
        )
        assert len(digest["key_items"]) == 8

    def test_non_dict_planning_data(self, tmp_path):
        agent = _agent(tmp_path)
        orch = _orch()
        orch.planning_data = "not a dict"
        assert agent._extract_plan_digest(orch) == {}

    def test_fallback_to_planning_summary(self, tmp_path):
        agent = _agent(tmp_path)
        orch = _orch(planning_data={}, summary="一份低脂晚餐方案。含更多细节。")
        advice_id = agent._maybe_record_advice(orch, "最终答案")
        # 没有 headline → 取 planning 摘要首句
        assert agent.advice_store.get("u1", advice_id).summary == "一份低脂晚餐方案"

    def test_fallback_to_final_answer(self, tmp_path):
        agent = _agent(tmp_path)
        orch = _orch(planning_data={}, summary="")
        advice_id = agent._maybe_record_advice(orch, "这是最终答案。后面还有。")
        assert agent.advice_store.get("u1", advice_id).summary == "这是最终答案"

    def test_no_summary_skips_record(self, tmp_path):
        agent = _agent(tmp_path)
        assert agent._maybe_record_advice(_orch(planning_data={}, summary=""), "") is None

    def test_summary_length_capped(self, tmp_path):
        agent = _agent(tmp_path)
        orch = _orch(planning_data={}, summary="长" * 500)
        advice_id = agent._maybe_record_advice(orch, "答案")
        assert len(agent.advice_store.get("u1", advice_id).summary) <= 120

    def test_planning_data_not_in_orchestration_dict(self, tmp_path):
        # 方案 §9.1：orchestration 响应字段必须保持二期形状
        orch = _orch(planning_data={"headline": "H", "final_answer": "很长的全文" * 100})
        assert "planning_data" not in orch.to_dict()


# ==================================================================
# 回访 system 注入（方案 §3.4 通道 A）
# ==================================================================
class TestFollowupInjection:
    def test_no_advice_means_no_injection(self, tmp_path):
        agent = _agent(tmp_path)
        # 硬保证：没有待回访建议时 system 消息与二期字节级一致
        assert agent._build_pending_advice_section() == ""

    def test_due_advice_injected(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent)
        block = agent._build_pending_advice_section()
        assert "待回访的历史建议" in block
        assert "7天减脂食谱" in block

    def test_advice_id_in_block(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-xyz")
        assert "a-xyz" in agent._build_pending_advice_section()

    def test_age_text_in_block(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, days_ago=3)
        assert "3天前" in agent._build_pending_advice_section()

    def test_not_due_advice_not_injected(self, tmp_path):
        agent = _agent(tmp_path)
        # 刚给完建议就追问「执行得怎么样」是很怪的体验
        _due_record(agent, days_ago=0)
        assert agent._build_pending_advice_section() == ""

    def test_answered_advice_not_injected(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        agent.advice_store.update_status("u1", "a-1", STATUS_ADOPTED)
        assert agent._build_pending_advice_section() == ""

    def test_push_mode_skips_injection(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent)
        agent._push_mode = True
        # 推送 prompt 自带回访段，重复注入会问两遍
        assert agent._build_pending_advice_section() == ""

    def test_no_memory_group_skips_injection(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent)
        agent.tool_groups = frozenset({"rag"})
        assert agent._build_pending_advice_section() == ""

    def test_limit_of_three(self, tmp_path):
        agent = _agent(tmp_path)
        for i in range(5):
            _due_record(agent, advice_id=f"a-{i}", summary=f"计划{i}", days_ago=5)
        assert len(agent._build_pending_advice_items()) == 3

    def test_store_failure_yields_empty(self, tmp_path):
        agent = _agent(tmp_path)

        class _BoomStore:
            def due_for_followup(self, *a, **k):
                raise RuntimeError("boom")

        agent.advice_store = _BoomStore()
        assert agent._build_pending_advice_items() == []

    def test_section_reaches_system_prompt(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent)
        assert "待回访的历史建议" in agent._build_system_prompt("你好")

    def test_system_prompt_clean_without_advice(self, tmp_path):
        agent = _agent(tmp_path)
        assert "待回访" not in agent._build_system_prompt("你好")


# ==================================================================
# advice_feedback 工具（方案 §3.5）
# ==================================================================
class TestAdviceFeedbackTool:
    def test_updates_status(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        result = agent._advice_feedback(advice_id="a-1", status="adopted", note="做了")
        assert result.startswith("✅")
        assert agent.advice_store.get("u1", "a-1").status == STATUS_ADOPTED

    def test_writes_episodic_memory(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        agent._advice_feedback(advice_id="a-1", status="adopted", note="很好吃")
        added = agent.service.memory_tool.memory_manager.added
        assert len(added) == 1
        assert added[0]["memory_type"] == "episodic"

    def test_memory_category_is_advice_feedback(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        agent._advice_feedback(advice_id="a-1", status="adopted")
        meta = agent.service.memory_tool.memory_manager.added[0]["metadata"]
        # 这条链路让反馈进入「提纯记忆摘要」注入，从而影响后续规划倾向
        assert meta["category"] == "advice_feedback"
        assert meta["advice_id"] == "a-1"

    def test_memory_content_includes_note(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        agent._advice_feedback(advice_id="a-1", status="partial", note="只做了三天")
        assert "只做了三天" in agent.service.memory_tool.memory_manager.added[0]["content"]

    @pytest.mark.parametrize(
        "status,expected",
        [("adopted", "采纳了"), ("rejected", "没有采纳"), ("partial", "部分采纳了")],
    )
    def test_status_rendered_in_memory(self, tmp_path, status, expected):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        agent._advice_feedback(advice_id="a-1", status=status)
        assert expected in agent.service.memory_tool.memory_manager.added[0]["content"]

    def test_blank_advice_id_uses_latest_pending(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-old", days_ago=9)
        _due_record(agent, advice_id="a-new", days_ago=2)
        agent._advice_feedback(status="adopted")
        # 用户说「那个食谱我试了」时模型给不出 id，须回落到最近一条
        assert agent.advice_store.get("u1", "a-new").status == STATUS_ADOPTED

    def test_no_pending_returns_warning(self, tmp_path):
        agent = _agent(tmp_path)
        assert agent._advice_feedback(status="adopted").startswith("⚠️")

    def test_unknown_advice_id_warns(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        assert agent._advice_feedback(advice_id="a-nope", status="adopted").startswith("⚠️")

    @pytest.mark.parametrize("status", ["", "done", "yes", "已采纳"])
    def test_invalid_status_warns(self, tmp_path, status):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        assert agent._advice_feedback(advice_id="a-1", status=status).startswith("⚠️")

    def test_invalid_status_does_not_write_memory(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        agent._advice_feedback(advice_id="a-1", status="bogus")
        assert agent.service.memory_tool.memory_manager.added == []

    def test_status_case_insensitive(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        assert agent._advice_feedback(advice_id="a-1", status=" Adopted ").startswith("✅")

    def test_trace_event_emitted(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        agent._advice_feedback(advice_id="a-1", status="adopted")
        assert "advice_feedback" in agent.trace_logger.names()

    def test_feedback_removes_from_followup_queue(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        agent._advice_feedback(advice_id="a-1", status="adopted")
        assert agent._build_pending_advice_section() == ""

    def test_memory_write_failure_does_not_break_tool(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")

        def _boom(**kwargs):
            raise RuntimeError("db down")

        agent.service.memory_tool.memory_manager.add_memory = _boom
        # 状态机已更新成功，记忆写入是第二条链路，失败不该让工具报错
        assert agent._advice_feedback(advice_id="a-1", status="adopted").startswith("✅")
        assert agent.advice_store.get("u1", "a-1").status == STATUS_ADOPTED

    def test_note_whitespace_collapsed(self, tmp_path):
        agent = _agent(tmp_path)
        _due_record(agent, advice_id="a-1")
        agent._advice_feedback(advice_id="a-1", status="partial", note="a\n\n  b")
        assert agent.advice_store.get("u1", "a-1").feedback_text == "a b"


# ==================================================================
# 工具注册
# ==================================================================
class TestToolRegistration:
    def test_registered_in_memory_group(self):
        from WellnessAgent.tools.registry import ToolRegistry

        agent = WellnessPlanningAgent.__new__(WellnessPlanningAgent)
        agent.tool_groups = frozenset({"memory"})
        registry = ToolRegistry()
        agent._register_tool_functions(registry)
        assert registry.has_tool("advice_feedback")

    def test_absent_in_rag_only_group(self):
        from WellnessAgent.tools.registry import ToolRegistry

        agent = WellnessPlanningAgent.__new__(WellnessPlanningAgent)
        agent.tool_groups = frozenset({"rag"})
        registry = ToolRegistry()
        agent._register_tool_functions(registry)
        assert not registry.has_tool("advice_feedback")

    def test_schema_has_three_params(self):
        from WellnessAgent.tools.registry import ToolRegistry

        agent = WellnessPlanningAgent.__new__(WellnessPlanningAgent)
        agent.tool_groups = frozenset({"memory"})
        registry = ToolRegistry()
        agent._register_tool_functions(registry)
        schema = next(
            s
            for s in registry.build_tool_schemas(include_finish=False)
            if s["function"]["name"] == "advice_feedback"
        )
        props = schema["function"]["parameters"]["properties"]
        assert set(props) == {"advice_id", "status", "note"}

    def test_readonly_subagents_cannot_call_it(self):
        """profile / safety 子代理是只读的，不该拿到这个写工具。"""
        from WellnessAgent.tools.tool_filter import ReadOnlyFilter

        # advice_feedback 会写 episodic 记忆，必须进只读黑名单
        assert ReadOnlyFilter().is_allowed("advice_feedback") is False


# ==================================================================
# 时间渲染
# ==================================================================
class TestHumanizeAge:
    def test_days(self):
        now = datetime(2026, 7, 28, 10, 0)
        assert _humanize_age(datetime(2026, 7, 25, 10, 0).isoformat(), now) == "3天前"

    def test_hours(self):
        now = datetime(2026, 7, 28, 10, 0)
        assert _humanize_age(datetime(2026, 7, 28, 5, 0).isoformat(), now) == "5小时前"

    def test_minutes(self):
        now = datetime(2026, 7, 28, 10, 0)
        assert _humanize_age(datetime(2026, 7, 28, 9, 30).isoformat(), now) == "30分钟前"

    def test_just_now(self):
        now = datetime(2026, 7, 28, 10, 0)
        assert _humanize_age(datetime(2026, 7, 28, 9, 59, 30).isoformat(), now) == "刚刚"

    @pytest.mark.parametrize("value", ["", "not-a-date", None])
    def test_invalid_returns_empty(self, value):
        assert _humanize_age(value) == ""

    def test_future_returns_empty(self):
        now = datetime(2026, 7, 28, 10, 0)
        assert _humanize_age(datetime(2026, 7, 29, 10, 0).isoformat(), now) == ""

