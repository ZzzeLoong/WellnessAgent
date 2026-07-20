"""Human-in-the-Loop 挂起态（WellnessAgent 自研，R7）。

方案 §3：采用**回合边界确认**（非"步骤中间真暂停"）。当编排 pipeline 命中高影响
步骤（画像敏感变更 / safety 命中过敏原）时，把"待确认项"作为本回合产物返回，回合
正常结束；挂起态 :class:`PendingConfirmation` 挂在 ``SessionStore.metadata`` 上衔接
前后两轮，下一轮 ``/api/chat`` 带 ``confirmation`` 恢复。

数据结构可序列化（``to_dict`` / ``from_dict``），不改一期消息序列化格式。
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# 确认类型（kind）。
KIND_PROFILE_UPDATE = "profile_update"  # 画像敏感字段变更建议
KIND_SAFETY_RISK = "safety_risk"  # safety 子代理命中风险

# 用户决策（decision）。
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_MODIFY = "modify"
VALID_DECISIONS = frozenset({DECISION_APPROVE, DECISION_REJECT, DECISION_MODIFY})


def new_confirm_id() -> str:
    """生成 ``c-YYYYMMDD-HHMMSS-xxxx`` 形式的确认 id。"""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"c-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{suffix}"


@dataclass
class PendingConfirmation:
    """一条待用户确认的高影响项（回合边界确认）。

    Attributes:
        confirm_id: 唯一确认 id（``c-...``），恢复时用于校验匹配。
        kind: 确认类型（``profile_update`` / ``safety_risk``）。
        prompt: 给用户看的确认问题（前端 ConfirmDialog 展示）。
        payload: 待确认内容（如 ``suggested_updates`` / safety ``hits``），
            以及恢复所需的编排上下文快照（如 ``draft_answer`` / ``message``）。
        created_at: 创建时间（ISO）。
    """

    confirm_id: str
    kind: str
    prompt: str
    payload: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirm_id": self.confirm_id,
            "kind": self.kind,
            "prompt": self.prompt,
            "payload": self.payload,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["PendingConfirmation"]:
        """从 dict 反序列化；缺少 confirm_id 视为无效返回 None。"""
        if not isinstance(data, dict):
            return None
        confirm_id = data.get("confirm_id")
        if not confirm_id:
            return None
        return cls(
            confirm_id=str(confirm_id),
            kind=str(data.get("kind", "")),
            prompt=str(data.get("prompt", "")),
            payload=dict(data.get("payload", {}) or {}),
            created_at=str(data.get("created_at", datetime.now().isoformat())),
        )

    def public_view(self) -> dict[str, Any]:
        """对外返回给前端的字段（不含恢复所需的内部快照）。"""
        payload = dict(self.payload or {})
        # 内部快照字段（恢复用）不必回传前端，保持契约精简。
        public_payload = {
            k: v
            for k, v in payload.items()
            if k not in {"draft_answer", "message", "subagent_results"}
        }
        return {
            "confirm_id": self.confirm_id,
            "kind": self.kind,
            "prompt": self.prompt,
            "payload": public_payload,
        }


@dataclass
class ConfirmationDecision:
    """用户对某个 PendingConfirmation 的响应（下一轮 ``confirmation`` 入参）。"""

    confirm_id: str
    decision: str  # approve | reject | modify
    patch: dict = field(default_factory=dict)  # decision==modify 时覆盖 payload

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["ConfirmationDecision"]:
        if not isinstance(data, dict):
            return None
        confirm_id = data.get("confirm_id")
        decision = str(data.get("decision", "")).strip().lower()
        if not confirm_id or decision not in VALID_DECISIONS:
            return None
        return cls(
            confirm_id=str(confirm_id),
            decision=decision,
            patch=dict(data.get("patch", {}) or {}),
        )

