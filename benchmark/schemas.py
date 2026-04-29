"""Core schemas for the standalone benchmark package."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


Difficulty = Literal["easy", "medium", "hard"]


class TaskTurn(BaseModel):
    """One conversational turn within a benchmark task."""

    role: Literal["user"] = "user"
    content: str


class TaskExpected(BaseModel):
    """Expected properties used by evaluators."""

    profile_fields: dict[str, Any] = Field(default_factory=dict)
    profile_must_not_contain: dict[str, Any] = Field(default_factory=dict)
    session_required: list[str] = Field(default_factory=list)
    forbidden_terms: list[str] = Field(default_factory=list)
    required_terms: list[str] = Field(default_factory=list)
    must_use_tools_any_of: list[str] = Field(default_factory=list)
    requires_replanning: bool = False
    requires_rag: bool = False
    required_knowledge_points: list[str] = Field(default_factory=list)


class TaskWeights(BaseModel):
    """Task-level metric weights."""

    hard_constraint: float = 0.0
    state_tracking: float = 0.0
    goal_alignment: float = 0.0
    replanning: float = 0.0
    rag_grounding: float = 0.0


class BenchmarkTask(BaseModel):
    """Benchmark task definition loaded from JSON."""

    task_id: str
    title: str
    difficulty: Difficulty
    category: list[str] = Field(default_factory=list)
    turns: list[TaskTurn]
    expected: TaskExpected = Field(default_factory=TaskExpected)
    weights: TaskWeights = Field(default_factory=TaskWeights)


class BenchmarkStep(BaseModel):
    """Normalized execution step."""

    step_index: int | None = None
    thought: str | None = None
    action_text: str | None = None
    tool_name: str | None = None
    tool_input: str | None = None
    observation: str | None = None
    raw_response: str | None = None
    action_debug: str | None = None


class BenchmarkTurnResult(BaseModel):
    """Normalized result for one executed turn."""

    turn_index: int
    user_message: str
    answer: str
    terminated_reason: str = "finished"
    steps: list[BenchmarkStep] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkRunResult(BaseModel):
    """Raw execution result for one task-baseline pair."""

    task_id: str
    baseline: str
    user_id: str
    started_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at: str | None = None
    turn_results: list[BenchmarkTurnResult] = Field(default_factory=list)
    final_state: dict[str, Any] = Field(default_factory=dict)


class MetricScores(BaseModel):
    """Per-metric deterministic scores.

    The `*_score` fields prefixed with the original 5 metric names are kept for
    backwards compatibility. New scores are added as separate fields and the
    aggregator is responsible for computing `total_score` only over the
    baseline-fair metrics so that `llm_only` is not implicitly punished for
    missing tool-aware capabilities.
    """

    hard_constraint_score: float = 0.0
    state_tracking_score: float = 0.0
    profile_tracking_score: float = 0.0
    session_tracking_score: float = 0.0
    goal_alignment_score: float = 0.0
    replanning_score: float = 0.0
    rag_grounding_score: float = 0.0
    knowledge_coverage_score: float = 0.0
    rag_invocation_score: float = 0.0
    tool_usage_score: float = 0.0
    total_score: float = 0.0


class BenchmarkScoreResult(BaseModel):
    """Persisted scoring result for one task-baseline pair."""

    task_id: str
    baseline: str
    difficulty: Difficulty
    scores: MetricScores
    metric_details: dict[str, Any] = Field(default_factory=dict)
    optional_judge: dict[str, Any] = Field(default_factory=dict)


class ReportPaths(BaseModel):
    """Filesystem locations used by the runner."""

    reports_dir: Path
    raw_runs_dir: Path
    scores_dir: Path
    summary_csv: Path
    summary_md: Path
    leaderboard_md: Path
    baselines_dir: Path
