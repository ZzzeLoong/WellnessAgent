"""Standard tool-response protocol (WellnessAgent self-contained).

This module defines a small, dependency-free protocol for tool results so the
ReAct loop always receives a structured object instead of a raw string or a
bare exception. It is inspired by mature agent frameworks but implemented for
WellnessAgent's own needs (see docs/tech-design-phase1.md, section 2.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ToolStatus(str, Enum):
    """Outcome status of a tool execution."""

    SUCCESS = "success"
    PARTIAL = "partial"
    ERROR = "error"


class ToolErrorCode:
    """Well-known, stable error codes for tool failures."""

    EXECUTION_ERROR = "EXECUTION_ERROR"
    INVALID_PARAM = "INVALID_PARAM"
    NOT_FOUND = "NOT_FOUND"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    TIMEOUT = "TIMEOUT"


@dataclass
class ToolResponse:
    """Structured result of a single tool invocation.

    Attributes:
        status: SUCCESS / PARTIAL / ERROR.
        text: Human/LLM readable text placed into the ``tool`` message.
        data: Optional structured payload for programmatic consumers.
        error_info: ``{"code": ..., "message": ...}`` when ``status == ERROR``.
        stats: Optional execution stats (e.g. latency).
    """

    status: ToolStatus
    text: str
    data: dict = field(default_factory=dict)
    error_info: Optional[dict] = None
    stats: Optional[dict] = None

    @property
    def is_error(self) -> bool:
        """Return True only for hard failures (ERROR)."""
        return self.status == ToolStatus.ERROR

    @property
    def is_success(self) -> bool:
        """Return True for SUCCESS results."""
        return self.status == ToolStatus.SUCCESS

    @classmethod
    def success(
        cls,
        text: str,
        data: Optional[dict] = None,
        stats: Optional[dict] = None,
    ) -> "ToolResponse":
        """Build a SUCCESS response."""
        return cls(
            status=ToolStatus.SUCCESS,
            text=text,
            data=data or {},
            stats=stats,
        )

    @classmethod
    def partial(
        cls,
        text: str,
        data: Optional[dict] = None,
        stats: Optional[dict] = None,
    ) -> "ToolResponse":
        """Build a PARTIAL response (usable but incomplete)."""
        return cls(
            status=ToolStatus.PARTIAL,
            text=text,
            data=data or {},
            stats=stats,
        )

    @classmethod
    def error(
        cls,
        code: str,
        message: str,
        stats: Optional[dict] = None,
    ) -> "ToolResponse":
        """Build an ERROR response with a stable code and message."""
        return cls(
            status=ToolStatus.ERROR,
            text=f"错误[{code}]：{message}",
            data={},
            error_info={"code": code, "message": message},
            stats=stats,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for trace/logging."""
        return {
            "status": self.status.value,
            "text": self.text,
            "data": self.data,
            "error_info": self.error_info,
            "stats": self.stats,
        }

