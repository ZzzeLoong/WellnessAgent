"""Deterministic replanning scoring."""

from __future__ import annotations

from typing import Any

from ..schemas import BenchmarkRunResult, BenchmarkTask


def score_replanning(task: BenchmarkTask, run: BenchmarkRunResult) -> tuple[float, dict[str, Any]]:
    """Approximate whether the agent actually revised its plan after late constraints."""
    if not task.expected.requires_replanning:
        return 1.0, {"applicable": False}

    if len(run.turn_results) < 2:
        return 0.0, {"applicable": True, "reason": "任务轮次不足，无法检测重规划"}

    first_answer = (run.turn_results[0].answer or "").strip()
    final_answer = (run.turn_results[-1].answer or "").strip()
    answer_changed = bool(first_answer and final_answer and first_answer != final_answer)

    required_tools = set(task.expected.must_use_tools_any_of)
    used_tools = {
        step.tool_name
        for turn in run.turn_results[1:]
        for step in turn.steps
        if step.tool_name
    }
    tool_condition = True if not required_tools else bool(required_tools & used_tools)

    score_parts = [1.0 if answer_changed else 0.0, 1.0 if tool_condition else 0.0]
    score = sum(score_parts) / len(score_parts)
    return score, {
        "applicable": True,
        "answer_changed": answer_changed,
        "required_tools": sorted(required_tools),
        "used_tools_after_first_turn": sorted(tool for tool in used_tools if tool),
        "tool_condition": tool_condition,
    }
