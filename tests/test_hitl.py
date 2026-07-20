"""core/hitl.py 单测（R7）：PendingConfirmation / ConfirmationDecision 序列化。"""

from WellnessAgent.core.hitl import (
    ConfirmationDecision,
    DECISION_APPROVE,
    KIND_PROFILE_UPDATE,
    KIND_SAFETY_RISK,
    PendingConfirmation,
    new_confirm_id,
)


class TestNewConfirmId:
    def test_prefix_and_uniqueness(self):
        a = new_confirm_id()
        b = new_confirm_id()
        assert a.startswith("c-")
        assert a != b


class TestPendingConfirmation:
    def test_roundtrip(self):
        pending = PendingConfirmation(
            confirm_id="c-x",
            kind=KIND_PROFILE_UPDATE,
            prompt="确认写入？",
            payload={"suggested_updates": {"allergies": ["花生"]}},
        )
        restored = PendingConfirmation.from_dict(pending.to_dict())
        assert restored is not None
        assert restored.confirm_id == "c-x"
        assert restored.kind == KIND_PROFILE_UPDATE
        assert restored.payload["suggested_updates"] == {"allergies": ["花生"]}

    def test_from_dict_invalid(self):
        assert PendingConfirmation.from_dict(None) is None
        assert PendingConfirmation.from_dict({}) is None
        assert PendingConfirmation.from_dict({"kind": "x"}) is None

    def test_public_view_hides_internal_snapshot(self):
        pending = PendingConfirmation(
            confirm_id="c-1",
            kind=KIND_SAFETY_RISK,
            prompt="风险确认",
            payload={
                "hits": ["花生"],
                "advice": "换成南瓜子",
                # 内部恢复快照字段不应对外暴露。
                "message": "原始请求",
                "draft_answer": "草稿",
                "subagent_results": [{"name": "planning"}],
            },
        )
        view = pending.public_view()
        assert view["confirm_id"] == "c-1"
        assert view["payload"]["hits"] == ["花生"]
        assert "message" not in view["payload"]
        assert "draft_answer" not in view["payload"]
        assert "subagent_results" not in view["payload"]


class TestConfirmationDecision:
    def test_valid(self):
        d = ConfirmationDecision.from_dict(
            {"confirm_id": "c-1", "decision": "approve", "patch": {"a": 1}}
        )
        assert d is not None
        assert d.confirm_id == "c-1"
        assert d.decision == DECISION_APPROVE
        assert d.patch == {"a": 1}

    def test_invalid_decision_rejected(self):
        assert ConfirmationDecision.from_dict({"confirm_id": "c", "decision": "maybe"}) is None
        assert ConfirmationDecision.from_dict({"decision": "approve"}) is None
        assert ConfirmationDecision.from_dict(None) is None

