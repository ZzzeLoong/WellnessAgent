"""Task loading helpers for benchmark JSON files."""

from __future__ import annotations

import json
from pathlib import Path

from .schemas import BenchmarkTask


TASKS_DIR = Path(__file__).resolve().parent / "tasks"


def _load_task(path: Path) -> BenchmarkTask:
    """Load a single benchmark task from JSON."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return BenchmarkTask.model_validate(payload)


def load_all_tasks(tasks_dir: Path | None = None) -> list[BenchmarkTask]:
    """Load all tasks sorted by file name."""
    base_dir = tasks_dir or TASKS_DIR
    tasks = [_load_task(path) for path in sorted(base_dir.glob("task_*.json"))]
    if not tasks:
        raise FileNotFoundError(f"未找到 benchmark 任务文件: {base_dir}")
    return tasks


def load_task_by_id(task_id: str, tasks_dir: Path | None = None) -> BenchmarkTask:
    """Load a single task by `task_id`."""
    normalized = task_id.strip()
    for task in load_all_tasks(tasks_dir=tasks_dir):
        if task.task_id == normalized:
            return task
    raise KeyError(f"未找到 benchmark 任务: {normalized}")
