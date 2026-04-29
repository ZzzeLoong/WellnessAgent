"""Deterministic state-tracking scoring (profile and session split)."""

from __future__ import annotations

from typing import Any

from ..schemas import BenchmarkRunResult, BenchmarkTask


def _normalize_scalar(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = [value]
    normalized = []
    for item in raw_items:
        text = _normalize_scalar(item)
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def score_profile_tracking(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Score profile_fields presence and profile_must_not_contain absence."""
    final_state = run.final_state or {}
    current_profile = final_state.get("current_profile", {}) or {}

    checks = 0
    passed = 0
    details: dict[str, Any] = {
        "applicable": True,
        "profile_field_checks": {},
        "profile_absence_checks": {},
    }

    for field_name, expected_value in task.expected.profile_fields.items():
        checks += 1
        if isinstance(expected_value, list):
            expected_items = _normalize_list(expected_value)
            actual_items = _normalize_list(current_profile.get(field_name, []))
            is_ok = all(item in actual_items for item in expected_items)
        else:
            actual_value = _normalize_scalar(current_profile.get(field_name, ""))
            expected_text = _normalize_scalar(expected_value)
            is_ok = actual_value == expected_text or expected_text in actual_value
        if is_ok:
            passed += 1
        details["profile_field_checks"][field_name] = is_ok

    for field_name, forbidden_value in task.expected.profile_must_not_contain.items():
        checks += 1
        if isinstance(forbidden_value, list):
            forbidden_items = _normalize_list(forbidden_value)
            actual_items = _normalize_list(current_profile.get(field_name, []))
            is_ok = all(item not in actual_items for item in forbidden_items)
        else:
            is_ok = _normalize_scalar(forbidden_value) not in _normalize_scalar(
                current_profile.get(field_name, "")
            )
        if is_ok:
            passed += 1
        details["profile_absence_checks"][field_name] = is_ok

    if checks == 0:
        details["applicable"] = False
        return 1.0, details

    details["passed"] = passed
    details["checks"] = checks
    return passed / checks, details


def score_session_tracking(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Score whether each session_required fragment ends up in working memory."""
    final_state = run.final_state or {}
    working_summary = _normalize_scalar(final_state.get("working_memory_summary", ""))

    expected_fragments = list(task.expected.session_required)
    if not expected_fragments:
        return 1.0, {"applicable": False, "session_checks": {}}

    session_checks: dict[str, bool] = {}
    passed = 0
    for fragment in expected_fragments:
        is_ok = _normalize_scalar(fragment) in working_summary
        session_checks[fragment] = is_ok
        if is_ok:
            passed += 1

    return passed / len(expected_fragments), {
        "applicable": True,
        "session_checks": session_checks,
        "passed": passed,
        "checks": len(expected_fragments),
    }


def score_state_tracking(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Combined state-tracking score kept for backwards compatibility.

    The aggregator now also reads `profile_tracking` and `session_tracking`
    individually; this function returns the average so that the existing
    `state_tracking` weight in tasks still works without rewrites.
    """
    profile_score, profile_details = score_profile_tracking(task, run)
    session_score, session_details = score_session_tracking(task, run)

    components: list[float] = []
    if profile_details.get("applicable", True):
        components.append(profile_score)
    if session_details.get("applicable", True):
        components.append(session_score)

    score = sum(components) / len(components) if components else 1.0
    details = {
        "profile": profile_details,
        "session": session_details,
        "applicable": bool(components),
    }
    return score, details
