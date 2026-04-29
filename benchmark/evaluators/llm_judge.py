"""Optional LLM judge hook for later qualitative analysis."""

from __future__ import annotations

from typing import Any

from ..schemas import BenchmarkRunResult, BenchmarkTask


def judge_run(task: BenchmarkTask, run: BenchmarkRunResult) -> dict[str, Any]:
    """Placeholder optional judge output.

    The first benchmark version keeps deterministic scoring as the primary signal.
    This hook is intentionally no-op so future work can plug in an LLM judge
    without changing runner or report schemas.
    """

    return {
        "enabled": False,
        "notes": "LLM judge 未启用；第一版 benchmark 仅使用 deterministic 评分。",
        "task_id": task.task_id,
        "baseline": run.baseline,
    }
