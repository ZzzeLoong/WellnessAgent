"""Deterministic RAG-related scoring split into invocation and coverage."""

from __future__ import annotations

from typing import Any

from ..schemas import BenchmarkRunResult, BenchmarkTask


KB_TOOL_NAMES = {"kb_search", "kb_answer"}


def score_rag_invocation(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Score whether the agent actually invoked KB tools when RAG is required.

    This metric is intentionally tool-aware: baselines without RAG tools score 0,
    which is the correct diagnostic for capability comparison. It is excluded
    from the weighted total to keep `llm_only` baselines fair.
    """
    if not task.expected.requires_rag:
        return 1.0, {"applicable": False}

    kb_tools_used = sorted(
        {
            step.tool_name
            for turn in run.turn_results
            for step in turn.steps
            if step.tool_name in KB_TOOL_NAMES
        }
    )
    score = 1.0 if kb_tools_used else 0.0
    return score, {
        "applicable": True,
        "kb_tools_used": kb_tools_used,
    }


def score_knowledge_coverage(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Score how many `required_knowledge_points` appear in the final answer.

    This metric is baseline-agnostic: it only inspects the final answer text,
    not which tools were used, so memory-only or RAG-only baselines are not
    structurally penalized.
    """
    if not task.expected.requires_rag and not task.expected.required_knowledge_points:
        return 1.0, {"applicable": False}

    final_answer = (run.turn_results[-1].answer if run.turn_results else "").lower()
    points = task.expected.required_knowledge_points
    if not points:
        return 1.0, {"applicable": False}

    point_hits = [point for point in points if point.lower() in final_answer]
    score = len(point_hits) / len(points)
    return score, {
        "applicable": True,
        "knowledge_point_hits": point_hits,
        "knowledge_points_total": list(points),
    }


def score_rag_grounding(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Backwards compatible RAG grounding aggregate of invocation and coverage."""
    invocation_score, invocation_details = score_rag_invocation(task, run)
    coverage_score, coverage_details = score_knowledge_coverage(task, run)

    components: list[float] = []
    if invocation_details.get("applicable", True):
        components.append(invocation_score)
    if coverage_details.get("applicable", True):
        components.append(coverage_score)

    score = sum(components) / len(components) if components else 1.0
    return score, {
        "applicable": bool(components),
        "invocation": invocation_details,
        "coverage": coverage_details,
    }
