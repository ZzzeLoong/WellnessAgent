"""Deterministic hard-constraint scoring."""

from __future__ import annotations

from typing import Any

from ..schemas import BenchmarkRunResult, BenchmarkTask


SAFE_CONTEXT_MARKERS = (
    "避免",
    "不要",
    "不含",
    "不能",
    "忌",
    "过敏",
    "排除",
    "避开",
    "remove",
    "avoid",
    "exclude",
    "allergy",
)


def _is_safe_mention(answer: str, term: str) -> bool:
    """Heuristically ignore forbidden terms mentioned as warnings, not recommendations."""
    index = answer.find(term)
    if index == -1:
        return False
    window = answer[max(0, index - 12) : min(len(answer), index + len(term) + 12)]
    return any(marker in window for marker in SAFE_CONTEXT_MARKERS)


def score_hard_constraints(task: BenchmarkTask, run: BenchmarkRunResult) -> tuple[float, dict[str, Any]]:
    """Score whether forbidden terms are absent from the final answer."""
    final_answer = (run.turn_results[-1].answer if run.turn_results else "").strip().lower()
    forbidden_hits = [
        term
        for term in task.expected.forbidden_terms
        if term.strip()
        and term.lower() in final_answer
        and not _is_safe_mention(final_answer, term.lower())
    ]
    score = 0.0 if forbidden_hits else 1.0
    return score, {"forbidden_hits": forbidden_hits}
