"""推送调度与内容生成单测（三期 R10 批次 3，方案 §4.1 / §4.3）。

两组重点：

1. **注入时钟的 ``run_due``**：不启动线程、不 sleep，直接喂 ``now`` 断言到期行为。
2. **会话隔离**（方案 §4.3 最关键的设计点）：推送轮跑在独立 session、跑完切回、
   不触发 distill、不污染用户的对话 messages。

agent 全部用桩，不碰真实 LLM / 记忆 / LRU 缓存。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from WellnessAgent.wellnessagent.push import (
    KIND_DAILY,
    KIND_FOLLOWUP,
    PushGenerator,
    PushInbox,
    PushPlanStore,
    _push_session,
)
from WellnessAgent.wellnessagent.scheduler import PushResult, PushScheduler
from WellnessAgent.wellnessagent.advice_store import AdviceRecord, AdviceStore


# ------------------------------------------------------------------ 桩
class _StubLLM:
    """假 LLM：返回预设文本并记录调用次数。"""

    def __init__(self, reply="今日建议\n\n多吃深色蔬菜，午餐加一份鸡胸。"):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class _StubService:
    def __init__(self, profile_summary="allergies: 花生", memory_summary="偏好清淡"):
        self._profile = profile_summary
        self._memory = memory_summary

    def get_current_profile_summary(self):
        return self._profile

    def get_distilled_memory_summary(self, limit=4):
        return self._memory


class _StubHistory:
    def __init__(self):
        self.history = []

    def set_history(self, messages):
        self.history = list(messages)


class _StubAgent:
    """最小 agent 桩：只提供 push 链路真正用到的成员。"""

    def __init__(self, tmp_path, user_id="u1", llm=None, service=None):
        self.user_id = user_id
        self.llm = llm or _StubLLM()
        self.service = service or _StubService()
        self.advice_store = AdviceStore(advice_dir=str(tmp_path / "advice"))
        self.trace_logger = _TraceRecorder()
        self.session_id = "s-user-active"
        self.messages = ["user-msg"]
        self.history_manager = _StubHistory()
        self._push_mode = False
        self.selected_sessions: list = []
        self.distill_calls = 0

    def select_session(self, session_id):
        self.selected_sessions.append(session_id)
        self.session_id = session_id

    def _handle_post_turn_memory(self):
        """模拟真实 agent 的 distill 入口（推送轮应当短路）。"""
        if self._push_mode:
            return
        self.distill_calls += 1


class _TraceRecorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def log_event(self, event, payload=None, step=None):
        self.events.append((event, payload or {}))

    def finalize(self):
        pass

    def names(self):
        return [e for e, _ in self.events]


@pytest.fixture()
def inbox(tmp_path):
    return PushInbox(push_dir=str(tmp_path / "push"))


@pytest.fixture()
def plans(tmp_path):
    return PushPlanStore(push_dir=str(tmp_path / "push"))


@pytest.fixture()
def agent(tmp_path):
    return _StubAgent(tmp_path)


def _due_advice(agent, summary="7天减脂食谱", created="2026-07-01T10:00:00"):
    agent.advice_store.append(
        AdviceRecord(
            advice_id="a-due-1",
            user_id=agent.user_id,
            summary=summary,
            created_at=created,
            followup_due_at="2026-07-02T10:00:00",
        )
    )


# ==================================================================
# 内容生成
# ==================================================================
class TestPushGenerator:
    def test_generates_daily_message(self, agent, inbox):
        message = PushGenerator(agent, inbox).generate(kinds=[KIND_DAILY])
        assert message is not None
        assert message.kind == KIND_DAILY
        assert message.body

    def test_title_body_split(self, agent, inbox):
        agent.llm = _StubLLM("今日建议\n\n多吃蔬菜。")
        message = PushGenerator(agent, inbox).generate(kinds=[KIND_DAILY])
        assert message.title == "今日建议"
        assert message.body == "多吃蔬菜。"

    def test_single_paragraph_falls_back_to_default_title(self, agent, inbox):
        agent.llm = _StubLLM("就一段话没有标题")
        message = PushGenerator(agent, inbox).generate(kinds=[KIND_DAILY])
        assert message.title == "今日饮食建议"
        assert message.body == "就一段话没有标题"

    def test_markdown_heading_stripped(self, agent, inbox):
        agent.llm = _StubLLM("## 今日建议\n\n正文内容")
        message = PushGenerator(agent, inbox).generate(kinds=[KIND_DAILY])
        assert message.title == "今日建议"

    def test_profile_reaches_prompt(self, agent, inbox):
        PushGenerator(agent, inbox).generate(kinds=[KIND_DAILY])
        prompt = agent.llm.calls[0][1]["content"]
        # 「个性化而非通用模板」的证据：画像必须进 prompt
        assert "花生" in prompt

    def test_memory_reaches_prompt(self, agent, inbox):
        PushGenerator(agent, inbox).generate(kinds=[KIND_DAILY])
        assert "偏好清淡" in agent.llm.calls[0][1]["content"]

    def test_followup_only_without_due_advice_returns_none(self, agent, inbox):
        # 只订阅回访但没有到期建议 → 不该硬造一条鸡汤打扰用户
        assert PushGenerator(agent, inbox).generate(kinds=[KIND_FOLLOWUP]) is None

    def test_followup_with_due_advice(self, agent, inbox):
        _due_advice(agent)
        message = PushGenerator(agent, inbox).generate(
            kinds=[KIND_FOLLOWUP], now=datetime(2026, 7, 5, 9, 0)
        )
        assert message is not None
        assert message.kind == KIND_FOLLOWUP
        assert message.ref_advice_id == "a-due-1"

    def test_combined_kind_when_both(self, agent, inbox):
        _due_advice(agent)
        message = PushGenerator(agent, inbox).generate(
            kinds=[KIND_DAILY, KIND_FOLLOWUP], now=datetime(2026, 7, 5, 9, 0)
        )
        assert message.kind == f"{KIND_DAILY}+{KIND_FOLLOWUP}"

    def test_pending_advice_reaches_prompt(self, agent, inbox):
        _due_advice(agent, summary="7天减脂食谱")
        PushGenerator(agent, inbox).generate(
            kinds=[KIND_DAILY, KIND_FOLLOWUP], now=datetime(2026, 7, 5, 9, 0)
        )
        assert "7天减脂食谱" in agent.llm.calls[0][1]["content"]

    def test_empty_kinds_returns_none(self, agent, inbox):
        assert PushGenerator(agent, inbox).generate(kinds=[]) is None

    def test_invalid_kinds_returns_none(self, agent, inbox):
        assert PushGenerator(agent, inbox).generate(kinds=["bogus"]) is None

    def test_llm_failure_returns_none(self, agent, inbox):
        agent.llm = _StubLLM(RuntimeError("boom"))
        assert PushGenerator(agent, inbox).generate(kinds=[KIND_DAILY]) is None

    def test_llm_failure_logs_error(self, agent, inbox):
        agent.llm = _StubLLM(RuntimeError("boom"))
        PushGenerator(agent, inbox).generate(kinds=[KIND_DAILY])
        assert "push_error" in agent.trace_logger.names()

    def test_empty_llm_reply_returns_none(self, agent, inbox):
        agent.llm = _StubLLM("   ")
        assert PushGenerator(agent, inbox).generate(kinds=[KIND_DAILY]) is None

    def test_missing_llm_returns_none(self, agent, inbox):
        agent.llm = None
        assert PushGenerator(agent, inbox).generate(kinds=[KIND_DAILY]) is None

    def test_generate_does_not_deliver(self, agent, inbox):
        PushGenerator(agent, inbox).generate(kinds=[KIND_DAILY])
        assert inbox.list("u1") == []


class TestPushDelivery:
    def test_deliver_writes_inbox(self, agent, inbox):
        gen = PushGenerator(agent, inbox)
        message = gen.generate(kinds=[KIND_DAILY])
        push_id = gen.deliver(message)
        assert [m.push_id for m in inbox.list("u1")] == [push_id]

    def test_deliver_logs_trace(self, agent, inbox):
        gen = PushGenerator(agent, inbox)
        gen.deliver(gen.generate(kinds=[KIND_DAILY]))
        assert "push_delivered" in agent.trace_logger.names()

    def test_followup_marks_advice_followed_up(self, agent, inbox):
        _due_advice(agent)
        gen = PushGenerator(agent, inbox)
        message = gen.generate(kinds=[KIND_FOLLOWUP], now=datetime(2026, 7, 5, 9, 0))
        gen.deliver(message)
        record = agent.advice_store.get("u1", "a-due-1")
        assert record.followed_up_at  # 明天不会再问同一条

    def test_followup_not_due_twice(self, agent, inbox):
        _due_advice(agent)
        gen = PushGenerator(agent, inbox)
        gen.deliver(gen.generate(kinds=[KIND_FOLLOWUP], now=datetime(2026, 7, 5, 9, 0)))
        assert gen.generate(kinds=[KIND_FOLLOWUP], now=datetime(2026, 7, 6, 9, 0)) is None

    def test_run_once_generates_and_delivers(self, agent, inbox):
        message = PushGenerator(agent, inbox).run_once(kinds=[KIND_DAILY])
        assert message is not None
        assert len(inbox.list("u1")) == 1

    def test_run_once_no_content_delivers_nothing(self, agent, inbox):
        assert PushGenerator(agent, inbox).run_once(kinds=[KIND_FOLLOWUP]) is None
        assert inbox.list("u1") == []


# ==================================================================
# 会话隔离（方案 §4.3 —— R10 最关键的设计点）
# ==================================================================
class TestSessionIsolation:
    def test_switches_to_push_session(self, agent, inbox):
        PushGenerator(agent, inbox).run_once(
            kinds=[KIND_DAILY], now=datetime(2026, 7, 28, 8, 30)
        )
        assert "push-20260728" in agent.selected_sessions

    def test_restores_original_session(self, agent, inbox):
        PushGenerator(agent, inbox).run_once(kinds=[KIND_DAILY])
        assert agent.session_id == "s-user-active"

    def test_restores_user_messages(self, agent, inbox):
        PushGenerator(agent, inbox).run_once(kinds=[KIND_DAILY])
        # 用户下次打开对话，历史里不能凭空多出几轮「自言自语」
        assert agent.messages == ["user-msg"]

    def test_push_mode_reset_after_run(self, agent, inbox):
        PushGenerator(agent, inbox).run_once(kinds=[KIND_DAILY])
        assert agent._push_mode is False

    def test_push_mode_active_during_run(self, agent, inbox):
        seen = {}

        original = agent.service.get_current_profile_summary

        def _spy():
            seen["push_mode"] = agent._push_mode
            return original()

        agent.service.get_current_profile_summary = _spy
        PushGenerator(agent, inbox).run_once(kinds=[KIND_DAILY])
        assert seen["push_mode"] is True

    def test_distill_skipped_in_push_mode(self, agent):
        with _push_session(agent):
            agent._handle_post_turn_memory()
        assert agent.distill_calls == 0

    def test_distill_works_outside_push_mode(self, agent):
        agent._handle_post_turn_memory()
        assert agent.distill_calls == 1

    def test_state_restored_on_exception(self, agent, inbox):
        agent.llm = _StubLLM(RuntimeError("boom"))
        PushGenerator(agent, inbox).run_once(kinds=[KIND_DAILY])
        # 即便生成过程抛异常，用户上下文也不能被留在推送 session 上
        assert agent.session_id == "s-user-active"
        assert agent._push_mode is False

    def test_context_manager_restores_none_session(self, tmp_path):
        agent = _StubAgent(tmp_path)
        agent.session_id = None
        with _push_session(agent):
            pass
        assert agent.session_id is None


# ==================================================================
# 调度器（注入时钟）
# ==================================================================
class TestPushScheduler:
    def _scheduler(self, agent, plans, inbox, now):
        return PushScheduler(
            agent_provider=lambda uid: agent,
            clock=lambda: now,
            plan_store=plans,
            inbox=inbox,
        )

    def test_run_due_skips_disabled(self, agent, plans, inbox):
        plans.update("u1", enabled=False)
        results = self._scheduler(
            agent, plans, inbox, datetime(2026, 7, 28, 12, 0)
        ).run_due()
        assert [r.reason for r in results] == ["disabled"]

    def test_run_due_skips_not_yet_due(self, agent, plans, inbox):
        plans.update("u1", enabled=True, daily_at="08:30")
        results = self._scheduler(
            agent, plans, inbox, datetime(2026, 7, 28, 7, 0)
        ).run_due()
        assert [r.reason for r in results] == ["not_due"]

    def test_run_due_delivers_when_due(self, agent, plans, inbox):
        plans.update("u1", enabled=True, daily_at="08:30")
        results = self._scheduler(
            agent, plans, inbox, datetime(2026, 7, 28, 8, 30)
        ).run_due()
        assert results[0].delivered is True
        assert len(inbox.list("u1")) == 1

    def test_run_due_marks_last_run_date(self, agent, plans, inbox):
        plans.update("u1", enabled=True, daily_at="08:30")
        self._scheduler(agent, plans, inbox, datetime(2026, 7, 28, 8, 30)).run_due()
        assert plans.load("u1").last_run_date == "2026-07-28"

    def test_no_duplicate_same_day(self, agent, plans, inbox):
        plans.update("u1", enabled=True, daily_at="08:30")
        scheduler = self._scheduler(agent, plans, inbox, datetime(2026, 7, 28, 8, 30))
        scheduler.run_due()
        scheduler.run_due(now=datetime(2026, 7, 28, 9, 0))
        assert len(inbox.list("u1")) == 1

    def test_delivers_again_next_day(self, agent, plans, inbox):
        plans.update("u1", enabled=True, daily_at="08:30")
        scheduler = self._scheduler(agent, plans, inbox, datetime(2026, 7, 28, 8, 30))
        scheduler.run_due()
        scheduler.run_due(now=datetime(2026, 7, 29, 8, 30))
        assert len(inbox.list("u1")) == 2

    def test_explicit_now_overrides_clock(self, agent, plans, inbox):
        plans.update("u1", enabled=True, daily_at="08:30")
        scheduler = self._scheduler(agent, plans, inbox, datetime(2026, 7, 28, 7, 0))
        results = scheduler.run_due(now=datetime(2026, 7, 28, 9, 0))
        assert results[0].delivered is True

    def test_no_plans_returns_empty(self, agent, plans, inbox):
        assert self._scheduler(agent, plans, inbox, datetime.now()).run_due() == []

    def test_last_results_recorded(self, agent, plans, inbox):
        plans.update("u1", enabled=True, daily_at="08:30")
        scheduler = self._scheduler(agent, plans, inbox, datetime(2026, 7, 28, 8, 30))
        scheduler.run_due()
        assert len(scheduler.last_results) == 1
        assert scheduler.last_tick_at

    def test_failure_isolated_per_user(self, tmp_path, plans, inbox):
        good = _StubAgent(tmp_path, user_id="u1")
        agents = {"u1": good}

        def provider(user_id):
            if user_id == "u2":
                raise RuntimeError("agent boom")
            return agents[user_id]

        plans.update("u1", enabled=True, daily_at="08:00")
        plans.update("u2", enabled=True, daily_at="08:00")
        scheduler = PushScheduler(
            agent_provider=provider, plan_store=plans, inbox=inbox
        )
        results = {r.user_id: r for r in scheduler.run_due(datetime(2026, 7, 28, 9, 0))}
        assert results["u1"].delivered is True
        assert results["u2"].delivered is False  # 单用户失败不拖垮其他人

    def test_generation_error_isolated(self, tmp_path, plans, inbox):
        agent = _StubAgent(tmp_path)
        agent.llm = _StubLLM(RuntimeError("boom"))
        plans.update("u1", enabled=True, daily_at="08:00")
        scheduler = PushScheduler(
            agent_provider=lambda uid: agent, plan_store=plans, inbox=inbox
        )
        results = scheduler.run_due(datetime(2026, 7, 28, 9, 0))
        assert results[0].delivered is False
        assert results[0].reason == "no_content"

    def test_run_now_ignores_enabled_flag(self, agent, plans, inbox):
        # demo 必需：不开全局调度也要能立刻演示一条推送
        plans.update("u1", enabled=False)
        scheduler = PushScheduler(
            agent_provider=lambda uid: agent, plan_store=plans, inbox=inbox
        )
        assert scheduler.run_now("u1").delivered is True

    def test_run_now_does_not_mark_by_default(self, agent, plans, inbox):
        plans.update("u1", enabled=True)
        scheduler = PushScheduler(
            agent_provider=lambda uid: agent, plan_store=plans, inbox=inbox
        )
        scheduler.run_now("u1", mark=False)
        assert plans.load("u1").last_run_date == ""

    def test_run_now_logs_scheduled_event(self, agent, plans, inbox):
        scheduler = PushScheduler(
            agent_provider=lambda uid: agent, plan_store=plans, inbox=inbox
        )
        scheduler.run_now("u1")
        assert "push_scheduled" in agent.trace_logger.names()

    def test_run_now_agent_unavailable(self, plans, inbox):
        def provider(_uid):
            raise RuntimeError("nope")

        scheduler = PushScheduler(
            agent_provider=provider, plan_store=plans, inbox=inbox
        )
        result = scheduler.run_now("u1")
        assert result.delivered is False
        assert "agent unavailable" in result.reason

    def test_plan_kinds_are_honored(self, agent, plans, inbox):
        plans.update("u1", enabled=True, daily_at="08:00", kinds=[KIND_FOLLOWUP])
        scheduler = PushScheduler(
            agent_provider=lambda uid: agent, plan_store=plans, inbox=inbox
        )
        # 只要 followup 且无到期建议 → 无内容
        assert scheduler.run_now("u1").reason == "no_content"


class TestSchedulerLifecycle:
    def test_start_returns_false_when_disabled(self, monkeypatch, plans, inbox):
        monkeypatch.setenv("WELLNESS_PUSH_ENABLED", "false")
        scheduler = PushScheduler(plan_store=plans, inbox=inbox)
        assert scheduler.start() is False
        assert scheduler.running is False

    def test_start_and_stop_when_enabled(self, monkeypatch, plans, inbox):
        monkeypatch.setenv("WELLNESS_PUSH_ENABLED", "true")
        scheduler = PushScheduler(plan_store=plans, inbox=inbox, tick_seconds=60)
        assert scheduler.start() is True
        assert scheduler.running is True
        scheduler.stop()
        assert scheduler.running is False

    def test_start_is_idempotent(self, monkeypatch, plans, inbox):
        monkeypatch.setenv("WELLNESS_PUSH_ENABLED", "true")
        scheduler = PushScheduler(plan_store=plans, inbox=inbox, tick_seconds=60)
        scheduler.start()
        try:
            assert scheduler.start() is False  # 已在运行
        finally:
            scheduler.stop()

    def test_thread_is_daemon(self, monkeypatch, plans, inbox):
        monkeypatch.setenv("WELLNESS_PUSH_ENABLED", "true")
        scheduler = PushScheduler(plan_store=plans, inbox=inbox, tick_seconds=60)
        scheduler.start()
        try:
            assert scheduler._thread.daemon is True
        finally:
            scheduler.stop()

    def test_stop_without_start_is_safe(self, plans, inbox):
        PushScheduler(plan_store=plans, inbox=inbox).stop()

    def test_tick_seconds_from_env(self, monkeypatch, plans, inbox):
        monkeypatch.setenv("WELLNESS_PUSH_TICK_SECONDS", "17")
        assert PushScheduler(plan_store=plans, inbox=inbox).tick_seconds == 17

    def test_invalid_tick_seconds_falls_back(self, monkeypatch, plans, inbox):
        monkeypatch.setenv("WELLNESS_PUSH_TICK_SECONDS", "junk")
        assert PushScheduler(plan_store=plans, inbox=inbox).tick_seconds == 60


class TestPushResult:
    def test_to_dict_keys(self):
        result = PushResult(user_id="u1", delivered=True, push_id="p-1", kind="daily")
        assert set(result.to_dict()) == {
            "user_id",
            "delivered",
            "push_id",
            "kind",
            "reason",
            "title",
        }

