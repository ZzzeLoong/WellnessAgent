"""Common baseline adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..schemas import BenchmarkRunResult, BenchmarkTask, BenchmarkTurnResult


class BaselineAdapter(ABC):
    """Unified interface used by the benchmark runner."""

    name: str

    def __init__(self, user_id: str):
        self.user_id = user_id

    @abstractmethod
    def setup(self) -> None:
        """Initialize runtime resources for the baseline."""

    @abstractmethod
    def reset(self) -> None:
        """Reset baseline state before a task run."""

    @abstractmethod
    def seed_knowledge_base(self) -> list[str]:
        """Prepare any required benchmark knowledge base."""

    @abstractmethod
    def run_turn(self, turn_index: int, user_message: str) -> BenchmarkTurnResult:
        """Execute a single user turn and return a normalized result."""

    @abstractmethod
    def get_final_state(self) -> dict[str, Any]:
        """Return the final baseline state after all turns."""

    @abstractmethod
    def teardown(self) -> None:
        """Release runtime resources for the baseline."""

    def run_task(self, task: BenchmarkTask) -> BenchmarkRunResult:
        """Default task execution loop shared by most baselines."""
        run_result = BenchmarkRunResult(
            task_id=task.task_id,
            baseline=self.name,
            user_id=self.user_id,
        )
        for turn_index, turn in enumerate(task.turns, start=1):
            turn_result = self.run_turn(turn_index, turn.content)
            run_result.turn_results.append(turn_result)
        run_result.final_state = self.get_final_state()
        return run_result
