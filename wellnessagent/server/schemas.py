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


class ChatRequest(BaseModel):
    """Chat request payload."""

    user_id: str = "web_user"
    message: str
    session_id: str | None = None
    # R7：对上一轮挂起项的确认恢复（可选）。
    confirmation: ConfirmationPayload | None = None


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
