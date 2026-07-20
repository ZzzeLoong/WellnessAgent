"""工具熔断器（WellnessAgent 自研，R16）。

状态机：Closed -> Open -> (recovery_timeout 到期自动) -> Closed。
仅真实异常（``ToolResponse.ERROR``）计入失败；业务工具的软失败文本（如以
``⚠️`` / ``错误：`` 开头但 status=SUCCESS）不计入，避免误熔断。
"""

from __future__ import annotations

import os
import time
from typing import Dict, Optional

from .response import ToolResponse, ToolStatus


class CircuitBreaker:
    """Per-tool circuit breaker."""

    def __init__(
        self,
        failure_threshold: Optional[int] = None,
        recovery_timeout: Optional[float] = None,
        enabled: Optional[bool] = None,
    ):
        self.failure_threshold = (
            failure_threshold
            if failure_threshold is not None
            else int(os.getenv("WELLNESS_CB_FAILURE_THRESHOLD", "3"))
        )
        self.recovery_timeout = (
            recovery_timeout
            if recovery_timeout is not None
            else float(os.getenv("WELLNESS_CB_RECOVERY_TIMEOUT", "300"))
        )
        if enabled is None:
            enabled = os.getenv("WELLNESS_CB_ENABLED", "true").lower() == "true"
        self.enabled = enabled

        self._failure_counts: Dict[str, int] = {}
        self._open_since: Dict[str, float] = {}

    def is_open(self, tool_name: str) -> bool:
        """Return True when the tool is currently short-circuited."""
        if not self.enabled:
            return False
        open_time = self._open_since.get(tool_name)
        if open_time is None:
            return False
        if (time.time() - open_time) >= self.recovery_timeout:
            # 恢复期到，自动关闭熔断。
            self.close(tool_name)
            return False
        return True

    def record_result(self, tool_name: str, response: ToolResponse) -> None:
        """Update counters from a tool result."""
        if not self.enabled:
            return
        if response.status == ToolStatus.ERROR:
            self._on_failure(tool_name)
        else:
            self._on_success(tool_name)

    def _on_failure(self, tool_name: str) -> None:
        count = self._failure_counts.get(tool_name, 0) + 1
        self._failure_counts[tool_name] = count
        if count >= self.failure_threshold:
            self._open_since.setdefault(tool_name, time.time())

    def _on_success(self, tool_name: str) -> None:
        self._failure_counts[tool_name] = 0
        self._open_since.pop(tool_name, None)

    def open(self, tool_name: str) -> None:
        """Manually open the breaker."""
        self._open_since[tool_name] = time.time()

    def close(self, tool_name: str) -> None:
        """Manually reset the breaker."""
        self._failure_counts[tool_name] = 0
        self._open_since.pop(tool_name, None)

    def get_status(self, tool_name: str) -> dict:
        """Return the current status for one tool."""
        open_time = self._open_since.get(tool_name)
        is_open = self.is_open(tool_name)
        recover_in = None
        if open_time is not None and is_open:
            recover_in = max(0.0, self.recovery_timeout - (time.time() - open_time))
        return {
            "tool": tool_name,
            "state": "open" if is_open else "closed",
            "failure_count": self._failure_counts.get(tool_name, 0),
            "open_since": open_time,
            "recover_in_seconds": recover_in,
        }

    def get_all_status(self) -> Dict[str, dict]:
        """Return status for every tool the breaker has seen."""
        names = set(self._failure_counts) | set(self._open_since)
        return {name: self.get_status(name) for name in names}

