"""Tool-invocation diagnostic scoring.

This is a **diagnostic** metric: tool-less baselines score 0, so the runner
keeps it out of the weighted total. It is still emitted in raw scores and
in the per-baseline details report to highlight capability differences.
"""

from __future__ import annotations

from typing import Any

from ..schemas import BenchmarkRunResult, BenchmarkTask


def score_tool_usage(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Score whether at least one `must_use_tools_any_of` was triggered."""
    required = [tool for tool in task.expected.must_use_tools_any_of if tool]
    if not required:
        return 1.0, {"applicable": False}

    invoked = sorted(
        {
            step.tool_name
            for turn in run.turn_results
            for step in turn.steps
            if step.tool_name
        }
    )
    matched = sorted(set(required) & set(invoked))
    score = 1.0 if matched else 0.0
    return score, {
        "applicable": True,
        "required_any_of": list(required),
        "actually_invoked": invoked,
        "matched": matched,
    }
