"""Pydantic schemas for the wellness API."""

from pydantic import BaseModel, Field


class ProfilePayload(BaseModel):
    """Structured profile payload accepted by the API."""

    allergies: list[str] = Field(default_factory=list)
    diet_pattern: str = "balanced"
    goal: str = "general_health"
    dislikes: list[str] = Field(default_factory=list)
    medical_notes: list[str] = Field(default_factory=list)
    preferred_cuisines: list[str] = Field(default_factory=list)
    cooking_constraints: list[str] = Field(default_factory=list)
    notes: str = ""


class ConfirmationPayload(BaseModel):
    """HITL 确认恢复入参（R7）。"""

    confirm_id: str
    decision: str  # approve | reject | modify
    patch: dict = Field(default_factory=dict)


class Attachment(BaseModel):
    """R9 多模态附件引用。

    只带 ``file_id``（``POST /api/upload`` 的返回值），**绝不**在请求体里传
    base64——原始字节已由 ``MediaStore`` 落盘，此处只是引用（方案 §2.5）。
    """

    file_id: str
    kind: str = "image"  # 预留 audio / video


class ChatRequest(BaseModel):
    """Chat request payload."""

    user_id: str = "web_user"
    message: str
    session_id: str | None = None
    # R7：对上一轮挂起项的确认恢复（可选）。
    confirmation: ConfirmationPayload | None = None
    # R9：本轮附件（可选）。为空时与二期行为字节级等价。
    attachments: list[Attachment] = Field(default_factory=list)


class ProfileRequest(BaseModel):
    """Profile update request payload."""

    user_id: str = "web_user"
    profile: ProfilePayload


class UserScopedRequest(BaseModel):
    """Simple user-scoped request payload."""

    user_id: str = "web_user"


class NewSessionRequest(BaseModel):
    """Request payload to start a fresh conversation session."""

    user_id: str = "web_user"


class AdviceFeedbackRequest(BaseModel):
    """R11 建议反馈打标入参（前端手动打标，便于 demo）。"""

    user_id: str = "web_user"
    advice_id: str = ""  # 留空表示最近一条待回访建议
    status: str  # adopted | rejected | partial
    note: str = ""


class PushPlanRequest(BaseModel):
    """R10 推送计划配置入参（满足验收「用户可开启关闭、调整频率」）。

    除 ``user_id`` 外全部可选：只传 ``enabled`` 即可开关，不必回传整份计划。
    """

    user_id: str = "web_user"
    enabled: bool | None = None
    daily_at: str | None = None  # "HH:MM"
    timezone: str | None = None
    kinds: list[str] | None = None  # daily_suggestion | followup


class PushReadRequest(BaseModel):
    """R10 推送已读标记入参。"""

    user_id: str = "web_user"
    push_id: str = ""
    all_read: bool = False
