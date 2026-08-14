"""AdviceStore 单测（三期 R11 批次 3，方案 §3.2）。

覆盖：

- append 与 ``followup_due_at`` 自动补齐；
- **append-only 更新语义**：读时按 ``advice_id`` 取最后一条；
- 状态机（含非法状态拒绝）；
- ``due_for_followup`` 的三条件判定（pending + 未回访 + 已到期）；
- 坏行容错、user 隔离、路径穿越防护。

全部走 ``tmp_path``，不碰真实 ``logs/advice``。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from WellnessAgent.wellnessagent.advice_store import (
    STATUS_ADOPTED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    STATUS_REJECTED,
    AdviceRecord,
    AdviceStore,
    new_advice_id,
)


@pytest.fixture()
def store(tmp_path):
    return AdviceStore(advice_dir=str(tmp_path / "advice"), followup_delay_hours=24)


def _record(user_id="u1", summary="7天减脂食谱", **kwargs) -> AdviceRecord:
    return AdviceRecord(
        advice_id=kwargs.pop("advice_id", "") or new_advice_id(),
        user_id=user_id,
        session_id=kwargs.pop("session_id", "s-1"),
        summary=summary,
        **kwargs,
    )


# ==================================================================
# id 生成
# ==================================================================
class TestNewAdviceId:
    def test_prefix_and_shape(self):
        advice_id = new_advice_id()
        assert advice_id.startswith("a-")
        # a-YYYYMMDD-HHMMSS-xxxx
        assert len(advice_id.split("-")) == 4

    def test_ids_are_unique(self):
        ids = {new_advice_id() for _ in range(50)}
        assert len(ids) == 50


# ==================================================================
# append
# ==================================================================
class TestAppend:
    def test_append_returns_id_and_persists(self, store):
        advice_id = store.append(_record())
        assert advice_id
        assert store.path_for("u1").exists()

    def test_followup_due_at_autofilled(self, store):
        created = datetime(2026, 7, 28, 10, 0, 0)
        record = _record(created_at=created.isoformat())
        store.append(record)
        due = datetime.fromisoformat(record.followup_due_at)
        assert due == created + timedelta(hours=24)

    def test_explicit_followup_due_at_kept(self, store):
        explicit = datetime(2026, 8, 1, 9, 0, 0).isoformat()
        record = _record(followup_due_at=explicit)
        store.append(record)
        assert record.followup_due_at == explicit

    def test_delay_hours_from_constructor(self, tmp_path):
        store = AdviceStore(advice_dir=str(tmp_path), followup_delay_hours=2)
        created = datetime(2026, 7, 28, 10, 0, 0)
        record = _record(created_at=created.isoformat())
        store.append(record)
        assert datetime.fromisoformat(record.followup_due_at) == created + timedelta(hours=2)

    def test_missing_id_is_generated(self, store):
        record = AdviceRecord(advice_id="", user_id="u1", summary="x")
        advice_id = store.append(record)
        assert advice_id.startswith("a-")

    def test_default_status_is_pending(self, store):
        advice_id = store.append(_record())
        assert store.get("u1", advice_id).status == STATUS_PENDING

    def test_long_summary_truncated(self, store):
        advice_id = store.append(_record(summary="很长" * 2000))
        assert len(store.get("u1", advice_id).summary) <= 600

    def test_one_line_per_append(self, store):
        store.append(_record())
        store.append(_record())
        lines = store.path_for("u1").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_plan_digest_roundtrip(self, store):
        digest = {"headline": "7天减脂", "days": 7, "meals": ["午餐"]}
        advice_id = store.append(_record(plan_digest=digest))
        assert store.get("u1", advice_id).plan_digest == digest


# ==================================================================
# append-only 更新语义
# ==================================================================
class TestAppendOnlySemantics:
    def test_latest_line_wins(self, store):
        advice_id = store.append(_record())
        store.update_status("u1", advice_id, STATUS_ADOPTED, feedback_text="做了")
        record = store.get("u1", advice_id)
        assert record.status == STATUS_ADOPTED
        assert record.feedback_text == "做了"

    def test_update_appends_not_rewrites(self, store):
        advice_id = store.append(_record())
        store.update_status("u1", advice_id, STATUS_ADOPTED)
        lines = store.path_for("u1").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2  # 原始行仍在，历史可追溯

    def test_list_returns_one_entry_per_id(self, store):
        advice_id = store.append(_record())
        store.update_status("u1", advice_id, STATUS_ADOPTED)
        store.update_status("u1", advice_id, STATUS_PARTIAL)
        assert len(store.list("u1")) == 1

    def test_creation_order_preserved_after_update(self, store):
        first = store.append(_record(summary="A", created_at="2026-07-01T10:00:00"))
        store.append(_record(summary="B", created_at="2026-07-02T10:00:00"))
        store.update_status("u1", first, STATUS_ADOPTED)
        # list 按创建时间倒序 → B 在前
        assert [r.summary for r in store.list("u1")] == ["B", "A"]


# ==================================================================
# 状态机
# ==================================================================
class TestUpdateStatus:
    @pytest.mark.parametrize(
        "status", [STATUS_ADOPTED, STATUS_REJECTED, STATUS_PARTIAL, STATUS_PENDING]
    )
    def test_valid_statuses_accepted(self, store, status):
        advice_id = store.append(_record())
        assert store.update_status("u1", advice_id, status) is True

    @pytest.mark.parametrize("status", ["done", "", "ADOPTED ", "unknown"])
    def test_invalid_status_rejected(self, store, status):
        advice_id = store.append(_record())
        assert store.update_status("u1", advice_id, status) is False

    def test_unknown_id_returns_false(self, store):
        store.append(_record())
        assert store.update_status("u1", "a-nope", STATUS_ADOPTED) is False

    def test_feedback_sets_followed_up_at(self, store):
        advice_id = store.append(_record())
        store.update_status("u1", advice_id, STATUS_ADOPTED)
        assert store.get("u1", advice_id).followed_up_at

    def test_empty_feedback_keeps_previous_text(self, store):
        advice_id = store.append(_record())
        store.update_status("u1", advice_id, STATUS_PARTIAL, feedback_text="一半")
        store.update_status("u1", advice_id, STATUS_ADOPTED)
        assert store.get("u1", advice_id).feedback_text == "一半"

    def test_updated_at_advances(self, store):
        advice_id = store.append(_record())
        before = store.get("u1", advice_id).updated_at
        store.update_status("u1", advice_id, STATUS_ADOPTED)
        assert store.get("u1", advice_id).updated_at >= before


# ==================================================================
# 查询
# ==================================================================
class TestList:
    def test_empty_when_no_file(self, store):
        assert store.list("ghost") == []

    def test_filter_by_single_status(self, store):
        adopted = store.append(_record(summary="A"))
        store.append(_record(summary="B"))
        store.update_status("u1", adopted, STATUS_ADOPTED)
        result = store.list("u1", status=STATUS_ADOPTED)
        assert [r.summary for r in result] == ["A"]

    def test_filter_by_multiple_statuses(self, store):
        a = store.append(_record(summary="A"))
        b = store.append(_record(summary="B"))
        store.update_status("u1", a, STATUS_ADOPTED)
        store.update_status("u1", b, STATUS_REJECTED)
        result = store.list("u1", status=[STATUS_ADOPTED, STATUS_REJECTED])
        assert len(result) == 2

    def test_newest_first(self, store):
        store.append(_record(summary="old", created_at="2026-07-01T10:00:00"))
        store.append(_record(summary="new", created_at="2026-07-05T10:00:00"))
        assert store.list("u1")[0].summary == "new"

    def test_limit_applied(self, store):
        for i in range(5):
            store.append(_record(summary=f"a{i}", created_at=f"2026-07-0{i+1}T10:00:00"))
        assert len(store.list("u1", limit=2)) == 2

    def test_users_are_isolated(self, store):
        store.append(_record(user_id="u1", summary="mine"))
        store.append(_record(user_id="u2", summary="yours"))
        assert [r.summary for r in store.list("u1")] == ["mine"]
        assert [r.summary for r in store.list("u2")] == ["yours"]

    def test_counts_by_status(self, store):
        a = store.append(_record())
        store.append(_record())
        store.update_status("u1", a, STATUS_ADOPTED)
        counts = store.counts_by_status("u1")
        assert counts[STATUS_ADOPTED] == 1
        assert counts[STATUS_PENDING] == 1

    def test_latest_pending(self, store):
        store.append(_record(summary="old", created_at="2026-07-01T10:00:00"))
        store.append(_record(summary="new", created_at="2026-07-05T10:00:00"))
        assert store.latest_pending("u1").summary == "new"

    def test_latest_pending_none_when_all_closed(self, store):
        advice_id = store.append(_record())
        store.update_status("u1", advice_id, STATUS_ADOPTED)
        assert store.latest_pending("u1") is None


# ==================================================================
# 回访到期判定
# ==================================================================
class TestDueForFollowup:
    def test_due_when_past_deadline(self, store):
        store.append(_record(created_at="2026-07-01T10:00:00"))
        due = store.due_for_followup("u1", now=datetime(2026, 7, 3, 10, 0))
        assert len(due) == 1

    def test_not_due_before_deadline(self, store):
        store.append(_record(created_at="2026-07-01T10:00:00"))
        due = store.due_for_followup("u1", now=datetime(2026, 7, 1, 12, 0))
        assert due == []

    def test_not_due_when_already_answered(self, store):
        advice_id = store.append(_record(created_at="2026-07-01T10:00:00"))
        store.update_status("u1", advice_id, STATUS_ADOPTED)
        assert store.due_for_followup("u1", now=datetime(2026, 7, 9, 10, 0)) == []

    def test_not_due_when_already_followed_up(self, store):
        advice_id = store.append(_record(created_at="2026-07-01T10:00:00"))
        store.mark_followed_up("u1", advice_id)
        assert store.due_for_followup("u1", now=datetime(2026, 7, 9, 10, 0)) == []

    def test_mark_followed_up_keeps_pending_status(self, store):
        advice_id = store.append(_record())
        store.mark_followed_up("u1", advice_id)
        # 发起回访 != 用户已反馈，状态必须仍是 pending
        assert store.get("u1", advice_id).status == STATUS_PENDING

    def test_mark_followed_up_unknown_id(self, store):
        assert store.mark_followed_up("u1", "a-nope") is False

    def test_earliest_due_first(self, store):
        store.append(_record(summary="later", created_at="2026-07-05T10:00:00"))
        store.append(_record(summary="earlier", created_at="2026-07-01T10:00:00"))
        due = store.due_for_followup("u1", now=datetime(2026, 7, 20, 10, 0))
        assert due[0].summary == "earlier"

    def test_limit_applied(self, store):
        for i in range(4):
            store.append(_record(created_at=f"2026-07-0{i+1}T10:00:00"))
        due = store.due_for_followup("u1", now=datetime(2026, 8, 1, 10, 0), limit=2)
        assert len(due) == 2

    def test_unparseable_due_date_is_not_due(self, store):
        record = _record(followup_due_at="not-a-date")
        store.append(record)
        # 宁可不打扰，也不要因脏数据把用户淹了
        assert store.due_for_followup("u1", now=datetime(2030, 1, 1)) == []


# ==================================================================
# 容错与安全
# ==================================================================
class TestRobustness:
    def test_corrupt_line_skipped(self, store):
        advice_id = store.append(_record(summary="good"))
        with open(store.path_for("u1"), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        records = store.list("u1")
        assert len(records) == 1
        assert records[0].advice_id == advice_id

    def test_line_without_advice_id_skipped(self, store):
        store.append(_record())
        with open(store.path_for("u1"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"user_id": "u1"}) + "\n")
        assert len(store.list("u1")) == 1

    def test_blank_lines_ignored(self, store):
        store.append(_record())
        with open(store.path_for("u1"), "a", encoding="utf-8") as fh:
            fh.write("\n\n")
        assert len(store.list("u1")) == 1

    def test_unknown_status_in_file_is_normalized(self, store):
        store.append(_record())  # 先建目录
        with open(store.path_for("u1"), "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"advice_id": "a-x", "user_id": "u1", "status": "weird"})
                + "\n"
            )
        assert store.get("u1", "a-x").status == STATUS_PENDING

    def test_non_dict_plan_digest_normalized(self, store):
        store.append(_record())
        with open(store.path_for("u1"), "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"advice_id": "a-y", "user_id": "u1", "plan_digest": "nope"})
                + "\n"
            )
        assert store.get("u1", "a-y").plan_digest == {}

    def test_get_empty_id_returns_none(self, store):
        assert store.get("u1", "") is None

    @pytest.mark.parametrize("user_id", ["../evil", "a/b", "..\\evil"])
    def test_path_traversal_neutralized(self, store, user_id):
        path = store.path_for(user_id)
        assert path.parent == store.advice_dir

    def test_summary_preview_truncates(self):
        record = AdviceRecord(advice_id="a-1", user_id="u1", summary="字" * 500)
        assert len(record.summary_preview(limit=50)) == 50

    def test_summary_preview_collapses_whitespace(self):
        record = AdviceRecord(advice_id="a-1", user_id="u1", summary="a\n\n  b")
        assert record.summary_preview() == "a b"

