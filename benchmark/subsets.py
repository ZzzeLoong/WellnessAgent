"""Curated benchmark task subsets used by `--limit` shortcuts.

The subsets prioritize coverage diversity (hard constraint, state tracking,
replanning, RAG grounding, multi-turn profile aggregation) and difficulty
balance (mix of easy/medium/hard) so smaller runs still produce meaningful
comparisons across baselines.
"""

from __future__ import annotations

from .schemas import BenchmarkTask


SMOKE_SUBSET: tuple[str, ...] = ("task_01",)
"""1-task smoke subset, used for the fastest sanity checks."""


CURATED_TEN: tuple[str, ...] = (
    "task_01",
    "task_05",
    "task_06",
    "task_09",
    "task_11",
    "task_13",
    "task_15",
    "task_16",
    "task_18",
    "task_20",
)
"""10-task curated subset balancing categories and difficulty.

Coverage breakdown:
    easy(1)   -> task_01
    medium(5) -> task_05, task_06, task_09, task_11, task_18
    hard(4)   -> task_13, task_15, task_16, task_20

Capability coverage:
    hard constraint        -> task_01, task_15
    profile state tracking -> task_05, task_06, task_11, task_16
    session state tracking -> task_09
    replanning             -> task_13, task_15, task_16
    RAG grounding          -> task_18, task_20
"""


def select_tasks_by_limit(
    tasks: list[BenchmarkTask],
    limit: int | None,
) -> list[BenchmarkTask]:
    """Filter tasks by curated subset for known limits, otherwise truncate by id order."""
    if not tasks:
        return []
    if limit is None:
        return tasks

    if limit == 1:
        wanted = SMOKE_SUBSET
    elif limit == 10:
        wanted = CURATED_TEN
    else:
        return [task for task in tasks][: max(0, limit)]

    indexed = {task.task_id: task for task in tasks}
    selected: list[BenchmarkTask] = []
    for task_id in wanted:
        if task_id in indexed:
            selected.append(indexed[task_id])
    return selected
