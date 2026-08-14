"""推送计划与收件箱单测（三期 R10 批次 3，方案 §4.2 / §4.4）。

覆盖 :class:`PushPlanStore` 的开关/时刻归一化/到期判定，与 :class:`PushInbox`
的 append-only 已读语义。全部走 ``tmp_path``，不碰真实 ``logs/push``。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from WellnessAgent.wellnessagent.push import (
    KIND_DAILY,
    KIND_FOLLOWUP,
    PushInbox,
    PushMessage,
    PushPlan,
    PushPlanStore,
    _normalize_time,
    new_push_id,
    push_enabled,
)


@pytest.fixture()
def plans(tmp_path):
    return PushPlanStore(push_dir=str(tmp_path / "push"))


@pytest.fixture()
def inbox(tmp_path):
    return PushInbox(push_dir=str(tmp_path / "push"))


def _message(user_id="u1", **kwargs) -> PushMessage:
    return PushMessage(
        push_id=kwargs.pop("push_id", "") or new_push_id(),
        user_id=user_id,
        kind=kwargs.pop("kind", KIND_DAILY),
        title=kwargs.pop("title", "今日建议"),
        body=kwargs.pop("body", "多吃蔬菜。"),
        **kwargs,
    )


# ==================================================================
# 总开关
# ==================================================================
class TestPushEnabled:
    def test_default_disabled(self, monkeypatch):
        monkeypatch.delenv("WELLNESS_PUSH_ENABLED", raising=False)
        assert push_enabled() is False

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("WELLNESS_PUSH_ENABLED", value)
        assert push_enabled() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "junk"])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("WELLNESS_PUSH_ENABLED", value)
        assert push_enabled() is False


# ==================================================================
# 时刻归一化
# ==================================================================
class TestNormalizeTime:
    @pytest.mark.parametrize(
        "raw,expected",
        [("8:30", "08:30"), ("08:30", "08:30"), ("23:59", "23:59"), ("0:0", "00:00")],
    )
    def test_valid(self, raw, expected):
        assert _normalize_time(raw) == expected

    @pytest.mark.parametrize("raw", ["", "abc", "25:00", "12:61", "12", "1:2:3", "-1:00"])
    def test_invalid_falls_back(self, raw):
        assert _normalize_time(raw, fallback="09:00") == "09:00"


# ==================================================================
# 推送计划
# ==================================================================
class TestPushPlanStore:
    def test_missing_plan_defaults_disabled(self, plans):
        plan = plans.load("u1")
        assert plan.enabled is False
        assert plan.user_id == "u1"

    def test_missing_plan_not_persisted(self, plans):
        plans.load("u1")
        assert not plans.path_for("u1").exists()

    def test_default_time_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WELLNESS_PUSH_DEFAULT_TIME", "07:15")
        store = PushPlanStore(push_dir=str(tmp_path))
        assert store.load("u1").daily_at == "07:15"

    def test_save_and_reload(self, plans):
        plans.save(PushPlan(user_id="u1", enabled=True, daily_at="09:00"))
        plan = plans.load("u1")
        assert plan.enabled is True
        assert plan.daily_at == "09:00"

    def test_update_toggles_enabled(self, plans):
        assert plans.update("u1", enabled=True).enabled is True
        assert plans.update("u1", enabled=False).enabled is False

    def test_update_normalizes_time(self, plans):
        assert plans.update("u1", daily_at="7:5").daily_at == "07:05"

    def test_update_ignores_invalid_kinds(self, plans):
        plan = plans.update("u1", kinds=["bogus"])
        # 非法 kinds 全被过滤 → 保留原有值而不是清空
        assert plan.kinds == [KIND_DAILY, KIND_FOLLOWUP]

    def test_update_accepts_valid_kinds(self, plans):
        assert plans.update("u1", kinds=[KIND_FOLLOWUP]).kinds == [KIND_FOLLOWUP]

    def test_partial_update_keeps_other_fields(self, plans):
        plans.update("u1", enabled=True, daily_at="09:30")
        plan = plans.update("u1", daily_at="10:00")
        assert plan.enabled is True  # 未传 enabled 不应被重置

    def test_none_enabled_is_ignored(self, plans):
        plans.update("u1", enabled=True)
        assert plans.update("u1", enabled=None).enabled is True

    def test_mark_ran_sets_date(self, plans):
        plan = plans.mark_ran("u1", when=date(2026, 7, 28))
        assert plan.last_run_date == "2026-07-28"

    def test_list_users(self, plans):
        plans.update("u1", enabled=True)
        plans.update("u2", enabled=True)
        assert sorted(plans.list_users()) == ["u1", "u2"]

    def test_list_users_empty(self, plans):
        assert plans.list_users() == []

    def test_corrupt_plan_file_falls_back(self, plans):
        path = plans.path_for("u1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert plans.load("u1").enabled is False

    @pytest.mark.parametrize("user_id", ["../evil", "a/b"])
    def test_path_traversal_neutralized(self, plans, user_id):
        assert plans.path_for(user_id).parent == plans.push_dir


# ==================================================================
# 到期判定
# ==================================================================
class TestPlanIsDue:
    def test_disabled_never_due(self):
        plan = PushPlan(user_id="u1", enabled=False, daily_at="08:30")
        assert plan.is_due(datetime(2026, 7, 28, 12, 0)) is False

    def test_due_after_target_time(self):
        plan = PushPlan(user_id="u1", enabled=True, daily_at="08:30")
        assert plan.is_due(datetime(2026, 7, 28, 8, 31)) is True

    def test_not_due_before_target_time(self):
        plan = PushPlan(user_id="u1", enabled=True, daily_at="08:30")
        assert plan.is_due(datetime(2026, 7, 28, 8, 29)) is False

    def test_due_exactly_at_target(self):
        plan = PushPlan(user_id="u1", enabled=True, daily_at="08:30")
        assert plan.is_due(datetime(2026, 7, 28, 8, 30)) is True

    def test_not_due_twice_same_day(self):
        plan = PushPlan(
            user_id="u1", enabled=True, daily_at="08:30", last_run_date="2026-07-28"
        )
        # tick 每 60s 一次，同日去重必须生效，否则 08:30–08:31 会连推两条
        assert plan.is_due(datetime(2026, 7, 28, 9, 0)) is False

    def test_due_again_next_day(self):
        plan = PushPlan(
            user_id="u1", enabled=True, daily_at="08:30", last_run_date="2026-07-28"
        )
        assert plan.is_due(datetime(2026, 7, 29, 8, 30)) is True


# ==================================================================
# 收件箱
# ==================================================================
class TestPushInbox:
    def test_append_and_list(self, inbox):
        push_id = inbox.append(_message())
        items = inbox.list("u1")
        assert len(items) == 1
        assert items[0].push_id == push_id

    def test_empty_inbox(self, inbox):
        assert inbox.list("ghost") == []
        assert inbox.unread_count("ghost") == 0

    def test_newest_first(self, inbox):
        inbox.append(_message(title="old", ts="2026-07-01T10:00:00"))
        inbox.append(_message(title="new", ts="2026-07-05T10:00:00"))
        assert inbox.list("u1")[0].title == "new"

    def test_unread_count(self, inbox):
        inbox.append(_message())
        inbox.append(_message())
        assert inbox.unread_count("u1") == 2

    def test_mark_read(self, inbox):
        push_id = inbox.append(_message())
        assert inbox.mark_read("u1", push_id) is True
        assert inbox.unread_count("u1") == 0

    def test_mark_read_is_append_only(self, inbox):
        push_id = inbox.append(_message())
        inbox.mark_read("u1", push_id)
        lines = inbox.path_for("u1").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert len(inbox.list("u1")) == 1  # 折叠为一条

    def test_mark_read_unknown_id(self, inbox):
        assert inbox.mark_read("u1", "p-nope") is False

    def test_mark_read_idempotent(self, inbox):
        push_id = inbox.append(_message())
        inbox.mark_read("u1", push_id)
        assert inbox.mark_read("u1", push_id) is True
        assert inbox.unread_count("u1") == 0

    def test_mark_all_read(self, inbox):
        inbox.append(_message())
        inbox.append(_message())
        assert inbox.mark_all_read("u1") == 2
        assert inbox.unread_count("u1") == 0

    def test_mark_all_read_skips_already_read(self, inbox):
        push_id = inbox.append(_message())
        inbox.append(_message())
        inbox.mark_read("u1", push_id)
        assert inbox.mark_all_read("u1") == 1

    def test_unread_only_filter(self, inbox):
        read_id = inbox.append(_message(title="read"))
        inbox.append(_message(title="unread"))
        inbox.mark_read("u1", read_id)
        items = inbox.list("u1", unread_only=True)
        assert [m.title for m in items] == ["unread"]

    def test_limit_applied(self, inbox):
        for i in range(5):
            inbox.append(_message(ts=f"2026-07-0{i+1}T10:00:00"))
        assert len(inbox.list("u1", limit=2)) == 2

    def test_users_isolated(self, inbox):
        inbox.append(_message(user_id="u1", title="mine"))
        inbox.append(_message(user_id="u2", title="yours"))
        assert [m.title for m in inbox.list("u1")] == ["mine"]

    def test_body_truncated(self, inbox):
        push_id = inbox.append(_message(body="长" * 5000))
        body = next(m.body for m in inbox.list("u1") if m.push_id == push_id)
        assert len(body) <= 2000

    def test_title_truncated(self, inbox):
        inbox.append(_message(title="标" * 200))
        assert len(inbox.list("u1")[0].title) <= 60

    def test_ref_advice_id_roundtrip(self, inbox):
        inbox.append(_message(kind=KIND_FOLLOWUP, ref_advice_id="a-123"))
        assert inbox.list("u1")[0].ref_advice_id == "a-123"

    def test_corrupt_line_skipped(self, inbox):
        inbox.append(_message())
        with open(inbox.path_for("u1"), "a", encoding="utf-8") as fh:
            fh.write("{bad json\n")
        assert len(inbox.list("u1")) == 1

    def test_line_without_push_id_skipped(self, inbox):
        inbox.append(_message())
        with open(inbox.path_for("u1"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"user_id": "u1"}) + "\n")
        assert len(inbox.list("u1")) == 1

    def test_generated_push_id_when_missing(self, inbox):
        message = PushMessage(
            push_id="", user_id="u1", kind=KIND_DAILY, title="t", body="b"
        )
        assert inbox.append(message).startswith("p-")


class TestNewPushId:
    def test_prefix(self):
        assert new_push_id().startswith("p-")

    def test_unique(self):
        assert len({new_push_id() for _ in range(50)}) == 50

