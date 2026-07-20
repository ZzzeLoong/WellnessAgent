"""tools/circuit_breaker.py 单元测试。"""

import time

import pytest

from tools.circuit_breaker import CircuitBreaker
from tools.response import ToolResponse, ToolStatus


class TestCircuitBreakerBasic:
    def test_initial_state_closed(self):
        cb = CircuitBreaker(failure_threshold=3, enabled=True)
        assert cb.is_open("tool_a") is False

    def test_disabled_always_closed(self):
        cb = CircuitBreaker(enabled=False)
        assert cb.is_open("any_tool") is False

    def test_disabled_does_not_record(self):
        cb = CircuitBreaker(enabled=False)
        resp = ToolResponse.error("ERR", "fail")
        cb.record_result("tool_a", resp)
        assert cb.is_open("tool_a") is False


class TestCircuitBreakerOpenClose:
    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=9999, enabled=True)
        for _ in range(3):
            cb.record_result("tool_a", ToolResponse.error("ERR", "fail"))
        assert cb.is_open("tool_a") is True

    def test_does_not_open_below_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, enabled=True)
        for _ in range(2):
            cb.record_result("tool_a", ToolResponse.error("ERR", "fail"))
        assert cb.is_open("tool_a") is False

    def test_success_resets_counter(self):
        cb = CircuitBreaker(failure_threshold=3, enabled=True)
        cb.record_result("tool_a", ToolResponse.error("ERR", "fail"))
        cb.record_result("tool_a", ToolResponse.error("ERR", "fail"))
        cb.record_result("tool_a", ToolResponse.success("ok"))
        cb.record_result("tool_a", ToolResponse.error("ERR", "fail"))
        cb.record_result("tool_a", ToolResponse.error("ERR", "fail"))
        assert cb.is_open("tool_a") is False

    def test_soft_failure_does_not_count(self):
        cb = CircuitBreaker(failure_threshold=2, enabled=True)
        cb.record_result("tool_a", ToolResponse.partial("some"))
        cb.record_result("tool_a", ToolResponse.partial("some"))
        assert cb.is_open("tool_a") is False

    def test_manual_open(self):
        cb = CircuitBreaker(failure_threshold=99, enabled=True)
        cb.open("tool_a")
        assert cb.is_open("tool_a") is True

    def test_manual_close(self):
        cb = CircuitBreaker(failure_threshold=3, enabled=True)
        for _ in range(3):
            cb.record_result("tool_a", ToolResponse.error("ERR", "fail"))
        assert cb.is_open("tool_a") is True
        cb.close("tool_a")
        assert cb.is_open("tool_a") is False


class TestCircuitBreakerRecovery:
    def test_auto_recovery_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1, enabled=True)
        cb.record_result("tool_a", ToolResponse.error("ERR", "fail"))
        assert cb.is_open("tool_a") is True
        time.sleep(0.15)
        assert cb.is_open("tool_a") is False

    def test_no_recovery_before_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=10, enabled=True)
        cb.record_result("tool_a", ToolResponse.error("ERR", "fail"))
        assert cb.is_open("tool_a") is True
        assert cb.is_open("tool_a") is True


class TestCircuitBreakerStatus:
    def test_get_status_closed(self):
        cb = CircuitBreaker(failure_threshold=3, enabled=True)
        status = cb.get_status("tool_a")
        assert status["state"] == "closed"
        assert status["failure_count"] == 0

    def test_get_status_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=9999, enabled=True)
        cb.record_result("tool_a", ToolResponse.error("ERR", "fail"))
        status = cb.get_status("tool_a")
        assert status["state"] == "open"
        assert status["failure_count"] >= 1

    def test_get_all_status(self):
        cb = CircuitBreaker(failure_threshold=1, enabled=True)
        cb.record_result("a", ToolResponse.error("ERR", "fail"))
        cb.record_result("b", ToolResponse.error("ERR", "fail"))
        all_status = cb.get_all_status()
        assert "a" in all_status
        assert "b" in all_status

    def test_recover_in_seconds_when_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=300, enabled=True)
        cb.record_result("tool_a", ToolResponse.error("ERR", "fail"))
        status = cb.get_status("tool_a")
        assert status["state"] == "open"
        assert status["recover_in_seconds"] is not None
        assert status["recover_in_seconds"] > 0

    def test_recover_in_seconds_when_closed(self):
        cb = CircuitBreaker(failure_threshold=3, enabled=True)
        status = cb.get_status("tool_a")
        assert status["state"] == "closed"
        assert status["recover_in_seconds"] is None

