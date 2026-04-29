"""Metric aggregation for benchmark runs.

The aggregator computes both the legacy 5 scores (so existing tasks keep
working without rewrites) and the new fine-grained scores. The weighted
`total_score` only mixes baseline-fair metrics so that LLM-only baselines are
not implicitly penalized for missing tool capabilities.

Baseline-fair metrics included in `total_score`:
  - hard_constraint_score   (weight: hard_constraint)
  - state_tracking_score    (weight: state_tracking)
  - goal_alignment_score    (weight: goal_alignment)
  - replanning_score        (weight: replanning)
  - knowledge_coverage_score(weight: rag_grounding)

Diagnostic metrics emitted but not weighted:
  - tool_usage_score
  - rag_invocation_score
  - profile_tracking_score / session_tracking_score (already folded into
    state_tracking_score; surfaced for analysis)
"""

from __future__ import annotations

from typing import Any

from ..schemas import BenchmarkRunResult, BenchmarkScoreResult, BenchmarkTask, MetricScores
from .hard_constraints import score_hard_constraints
from .rag_grounding import (
    score_knowledge_coverage,
    score_rag_grounding,
    score_rag_invocation,
)
from .replanning import score_replanning
from .state_tracking import (
    score_profile_tracking,
    score_session_tracking,
    score_state_tracking,
)
from .tool_usage import score_tool_usage


def score_goal_alignment(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Approximate goal alignment via required term coverage.

    When `required_terms` is empty we treat the task as not goal-aligned-checked
    and return 1.0 (so empty-list tasks do not collapse the weighted average).
    """
    final_answer = (run.turn_results[-1].answer if run.turn_results else "").lower()
    required_terms = [term for term in task.expected.required_terms if term.strip()]
    if not required_terms:
        return 1.0, {"applicable": False, "required_terms": [], "hits": []}
    hits = [term for term in required_terms if term.lower() in final_answer]
    score = len(hits) / len(required_terms)
    return score, {
        "applicable": True,
        "required_terms": required_terms,
        "hits": hits,
    }


def _weighted_average(task: BenchmarkTask, metrics: dict[str, float]) -> tuple[float, dict[str, Any]]:
    """Compute the baseline-fair weighted average and surface its breakdown."""
    weights = {
        "hard_constraint_score": task.weights.hard_constraint,
        "state_tracking_score": task.weights.state_tracking,
        "goal_alignment_score": task.weights.goal_alignment,
        "replanning_score": task.weights.replanning,
        "knowledge_coverage_score": task.weights.rag_grounding,
    }
    breakdown: list[dict[str, float]] = []
    numerator = 0.0
    denominator = 0.0
    for metric_name, weight in weights.items():
        if weight <= 0:
            continue
        score_value = metrics.get(metric_name, 0.0)
        numerator += weight * score_value
        denominator += weight
        breakdown.append(
            {
                "metric": metric_name,
                "weight": weight,
                "score": score_value,
                "weighted": weight * score_value,
            }
        )
    total = numerator / denominator if denominator else 0.0
    return total, {"breakdown": breakdown, "denominator": denominator}


def score_run(task: BenchmarkTask, run: BenchmarkRunResult) -> BenchmarkScoreResult:
    """Compute deterministic benchmark scores for one run."""
    hard_score, hard_details = score_hard_constraints(task, run)
    state_score, state_details = score_state_tracking(task, run)
    profile_score, profile_details = score_profile_tracking(task, run)
    session_score, session_details = score_session_tracking(task, run)
    goal_score, goal_details = score_goal_alignment(task, run)
    replanning_score, replanning_details = score_replanning(task, run)
    rag_grounding_score, rag_grounding_details = score_rag_grounding(task, run)
    rag_invocation_score, rag_invocation_details = score_rag_invocation(task, run)
    knowledge_coverage_score, knowledge_coverage_details = score_knowledge_coverage(task, run)
    tool_usage_score, tool_usage_details = score_tool_usage(task, run)

    metrics = {
        "hard_constraint_score": hard_score,
        "state_tracking_score": state_score,
        "profile_tracking_score": profile_score,
        "session_tracking_score": session_score,
        "goal_alignment_score": goal_score,
        "replanning_score": replanning_score,
        "rag_grounding_score": rag_grounding_score,
        "rag_invocation_score": rag_invocation_score,
        "knowledge_coverage_score": knowledge_coverage_score,
        "tool_usage_score": tool_usage_score,
    }
    total_score, total_breakdown = _weighted_average(task, metrics)

    return BenchmarkScoreResult(
        task_id=task.task_id,
        baseline=run.baseline,
        difficulty=task.difficulty,
        scores=MetricScores(total_score=total_score, **metrics),
        metric_details={
            "hard_constraint": hard_details,
            "state_tracking": state_details,
            "profile_tracking": profile_details,
            "session_tracking": session_details,
            "goal_alignment": goal_details,
            "replanning": replanning_details,
            "rag_grounding": rag_grounding_details,
            "rag_invocation": rag_invocation_details,
            "knowledge_coverage": knowledge_coverage_details,
            "tool_usage": tool_usage_details,
            "total": total_breakdown,
        },
    )
