"""Benchmark runner for executing tasks and writing reports."""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .baselines import (
    ALL_BASELINES,
    FullAgentBaseline,
    LLMMemoryBaseline,
    LLMOnlyBaseline,
    LLMRagBaseline,
)
from .baselines.base import BaselineAdapter
from .evaluators.aggregate import score_run
from .evaluators.llm_judge import judge_run
from .schemas import (
    BenchmarkRunResult,
    BenchmarkScoreResult,
    BenchmarkTask,
    MetricScores,
    ReportPaths,
)
from .utils import load_repo_env


METRIC_DISPLAY_NAMES: dict[str, str] = {
    "hard_constraint_score": "Hard",
    "state_tracking_score": "State",
    "profile_tracking_score": "Profile",
    "session_tracking_score": "Session",
    "goal_alignment_score": "Goal",
    "replanning_score": "Replan",
    "rag_grounding_score": "RAG",
    "knowledge_coverage_score": "Know",
    "rag_invocation_score": "RAGUse",
    "tool_usage_score": "ToolUse",
    "total_score": "Total",
}

WEIGHTED_METRIC_NAMES: tuple[str, ...] = (
    "hard_constraint_score",
    "state_tracking_score",
    "goal_alignment_score",
    "replanning_score",
    "knowledge_coverage_score",
)

DIAGNOSTIC_METRIC_NAMES: tuple[str, ...] = (
    "profile_tracking_score",
    "session_tracking_score",
    "rag_grounding_score",
    "rag_invocation_score",
    "tool_usage_score",
)


class BenchmarkRunner:
    """Execute benchmark tasks against selected baselines."""

    def __init__(
        self,
        reports_dir: Path | None = None,
        verbose: bool = True,
        show_progress: bool = True,
    ):
        self.base_dir = Path(__file__).resolve().parent
        self.reports_dir = reports_dir or self.base_dir / "reports"
        self.verbose = verbose
        self.show_progress = show_progress

    def build_report_paths(self) -> ReportPaths:
        """Prepare report output directories."""
        raw_runs_dir = self.reports_dir / "raw_runs"
        scores_dir = self.reports_dir / "scores"
        baselines_dir = self.reports_dir / "baselines"
        raw_runs_dir.mkdir(parents=True, exist_ok=True)
        scores_dir.mkdir(parents=True, exist_ok=True)
        baselines_dir.mkdir(parents=True, exist_ok=True)
        return ReportPaths(
            reports_dir=self.reports_dir,
            raw_runs_dir=raw_runs_dir,
            scores_dir=scores_dir,
            summary_csv=self.reports_dir / "summary.csv",
            summary_md=self.reports_dir / "summary.md",
            leaderboard_md=self.reports_dir / "leaderboard.md",
            baselines_dir=baselines_dir,
        )

    def create_baseline(self, baseline_name: str, user_id: str) -> BaselineAdapter:
        """Instantiate a supported baseline."""
        if baseline_name == "full_agent":
            return FullAgentBaseline(user_id=user_id)
        if baseline_name == "llm_memory":
            return LLMMemoryBaseline(user_id=user_id)
        if baseline_name == "llm_rag":
            return LLMRagBaseline(user_id=user_id)
        if baseline_name == "llm_only":
            return LLMOnlyBaseline(user_id=user_id)
        raise ValueError(
            f"不支持的 baseline: {baseline_name}. 可选：{', '.join(ALL_BASELINES)}"
        )

    def run(
        self,
        tasks: Iterable[BenchmarkTask],
        baselines: list[str],
    ) -> list[BenchmarkScoreResult]:
        """Run selected tasks against selected baselines."""
        load_repo_env()
        paths = self.build_report_paths()
        tasks_list = list(tasks)
        score_results: list[BenchmarkScoreResult] = []
        run_results: list[BenchmarkRunResult] = []

        total_units = max(1, len(baselines) * len(tasks_list))
        unit_index = 0
        suite_started_at = time.time()

        for baseline_name in baselines:
            self._announce_baseline(baseline_name, len(tasks_list))
            for task in tasks_list:
                unit_index += 1
                user_id = self._build_user_id(baseline_name, task.task_id)
                baseline = self.create_baseline(baseline_name, user_id=user_id)
                turn_started_at = time.time()
                self._announce_task_start(
                    baseline_name=baseline_name,
                    task=task,
                    unit_index=unit_index,
                    total_units=total_units,
                )
                try:
                    baseline.setup()
                    baseline.reset()
                    baseline.seed_knowledge_base()
                    run_result = baseline.run_task(task)
                    run_result.finished_at = datetime.utcnow().isoformat()

                    score_result = score_run(task, run_result)
                    score_result.optional_judge = judge_run(task, run_result)
                    status = "ok"
                except Exception as exc:
                    run_result, score_result = self._build_failure_result(
                        task=task,
                        baseline_name=baseline_name,
                        user_id=user_id,
                        error=exc,
                    )
                    status = f"error: {exc}"
                finally:
                    try:
                        baseline.teardown()
                    except Exception:
                        pass

                duration_seconds = time.time() - turn_started_at
                score_results.append(score_result)
                run_results.append(run_result)
                self._write_run(paths, run_result)
                self._write_score(paths, score_result)
                self._announce_task_end(
                    baseline_name=baseline_name,
                    task=task,
                    score=score_result,
                    status=status,
                    duration_seconds=duration_seconds,
                )

        suite_duration = time.time() - suite_started_at
        self._write_summary_csv(paths, score_results)
        self._write_summary_md(paths, score_results)
        self._write_leaderboard_md(paths, score_results)
        self._write_baseline_details(paths, score_results, run_results)

        if self.verbose:
            print(
                f"\n[benchmark] 全部任务完成。共 {len(score_results)} 个 (task,baseline) 对，"
                f"耗时 {suite_duration:.1f}s。报告输出：{self.reports_dir}"
            )

        return score_results

    def _announce_baseline(self, baseline_name: str, task_count: int) -> None:
        if not self.verbose:
            return
        print(f"\n[benchmark] === Baseline: {baseline_name} (将执行 {task_count} 个任务) ===")

    def _announce_task_start(
        self,
        baseline_name: str,
        task: BenchmarkTask,
        unit_index: int,
        total_units: int,
    ) -> None:
        if not self.show_progress:
            return
        print(
            f"[benchmark] [{unit_index:03d}/{total_units:03d}] "
            f"{baseline_name} :: {task.task_id} ({task.difficulty}) "
            f"\"{task.title}\" -> running..."
        )

    def _announce_task_end(
        self,
        baseline_name: str,
        task: BenchmarkTask,
        score: BenchmarkScoreResult,
        status: str,
        duration_seconds: float,
    ) -> None:
        if not self.verbose:
            return

        scores = score.scores
        compact = (
            f"hard={scores.hard_constraint_score:.2f} "
            f"state={scores.state_tracking_score:.2f} "
            f"goal={scores.goal_alignment_score:.2f} "
            f"replan={scores.replanning_score:.2f} "
            f"know={scores.knowledge_coverage_score:.2f} "
            f"tool={scores.tool_usage_score:.2f} "
            f"rag_use={scores.rag_invocation_score:.2f} "
            f"total={scores.total_score:.2f}"
        )
        print(
            f"[benchmark]    -> done {baseline_name}::{task.task_id} | "
            f"{compact} | status={status} | {duration_seconds:.1f}s"
        )

    def _build_user_id(self, baseline_name: str, task_id: str) -> str:
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        return f"bench_{baseline_name}_{task_id}_{timestamp}"

    def _write_run(self, paths: ReportPaths, run_result: BenchmarkRunResult) -> None:
        baseline_dir = paths.raw_runs_dir / run_result.baseline
        baseline_dir.mkdir(parents=True, exist_ok=True)
        target = baseline_dir / f"{run_result.task_id}.json"
        target.write_text(
            json.dumps(run_result.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_score(self, paths: ReportPaths, score_result: BenchmarkScoreResult) -> None:
        baseline_dir = paths.scores_dir / score_result.baseline
        baseline_dir.mkdir(parents=True, exist_ok=True)
        target = baseline_dir / f"{score_result.task_id}.json"
        target.write_text(
            json.dumps(score_result.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_summary_csv(
        self,
        paths: ReportPaths,
        results: list[BenchmarkScoreResult],
    ) -> None:
        with paths.summary_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "task_id",
                    "baseline",
                    "difficulty",
                    "hard_constraint_score",
                    "state_tracking_score",
                    "profile_tracking_score",
                    "session_tracking_score",
                    "goal_alignment_score",
                    "replanning_score",
                    "rag_grounding_score",
                    "knowledge_coverage_score",
                    "rag_invocation_score",
                    "tool_usage_score",
                    "total_score",
                ]
            )
            for item in results:
                scores = item.scores
                writer.writerow(
                    [
                        item.task_id,
                        item.baseline,
                        item.difficulty,
                        scores.hard_constraint_score,
                        scores.state_tracking_score,
                        scores.profile_tracking_score,
                        scores.session_tracking_score,
                        scores.goal_alignment_score,
                        scores.replanning_score,
                        scores.rag_grounding_score,
                        scores.knowledge_coverage_score,
                        scores.rag_invocation_score,
                        scores.tool_usage_score,
                        scores.total_score,
                    ]
                )

    def _write_summary_md(
        self,
        paths: ReportPaths,
        results: list[BenchmarkScoreResult],
    ) -> None:
        lines = [
            "# Benchmark Summary",
            "",
            "本表展示每个 `(task, baseline)` 对的所有原始指标。`Total` 仅由对所有 baseline 公平的指标加权得到，",
            "`ToolUse` 与 `RAGUse` 是诊断性指标，便于分析能力差异。",
            "",
            (
                "| Task | Baseline | Difficulty | Hard | State | Profile | Session | "
                "Goal | Replan | Know | RAGUse | ToolUse | Total |"
            ),
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for item in results:
            scores = item.scores
            lines.append(
                f"| {item.task_id} | {item.baseline} | {item.difficulty} | "
                f"{scores.hard_constraint_score:.2f} | "
                f"{scores.state_tracking_score:.2f} | "
                f"{scores.profile_tracking_score:.2f} | "
                f"{scores.session_tracking_score:.2f} | "
                f"{scores.goal_alignment_score:.2f} | "
                f"{scores.replanning_score:.2f} | "
                f"{scores.knowledge_coverage_score:.2f} | "
                f"{scores.rag_invocation_score:.2f} | "
                f"{scores.tool_usage_score:.2f} | "
                f"{scores.total_score:.2f} |"
            )
        lines.append("")
        paths.summary_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_leaderboard_md(
        self,
        paths: ReportPaths,
        results: list[BenchmarkScoreResult],
    ) -> None:
        if not results:
            paths.leaderboard_md.write_text(
                "# Benchmark Leaderboard\n\n暂无结果。\n", encoding="utf-8"
            )
            return

        per_baseline: dict[str, dict[str, list[float]]] = {}
        for item in results:
            bucket = per_baseline.setdefault(
                item.baseline,
                {metric: [] for metric in METRIC_DISPLAY_NAMES.keys()},
            )
            for metric_name in METRIC_DISPLAY_NAMES.keys():
                bucket[metric_name].append(getattr(item.scores, metric_name, 0.0))

        rows: list[dict[str, float | str]] = []
        for baseline_name, metrics in per_baseline.items():
            row: dict[str, float | str] = {"baseline": baseline_name}
            for metric_name, values in metrics.items():
                row[metric_name] = sum(values) / len(values) if values else 0.0
            rows.append(row)
        rows.sort(key=lambda row: row.get("total_score", 0.0), reverse=True)

        lines = [
            "# Benchmark Leaderboard",
            "",
            "按 `total_score` 平均分降序排序。`total_score` 仅基于公平指标，",
            "`ToolUse` / `RAGUse` 列为诊断性指标，便于分析能力差异。",
            "",
            (
                "| Rank | Baseline | Total | Hard | State | Profile | Session | "
                "Goal | Replan | Know | RAGUse | ToolUse | Tasks |"
            ),
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for rank, row in enumerate(rows, start=1):
            tasks_count = len(per_baseline[row["baseline"]]["total_score"])
            lines.append(
                f"| {rank} | {row['baseline']} | "
                f"{row['total_score']:.2f} | "
                f"{row['hard_constraint_score']:.2f} | "
                f"{row['state_tracking_score']:.2f} | "
                f"{row['profile_tracking_score']:.2f} | "
                f"{row['session_tracking_score']:.2f} | "
                f"{row['goal_alignment_score']:.2f} | "
                f"{row['replanning_score']:.2f} | "
                f"{row['knowledge_coverage_score']:.2f} | "
                f"{row['rag_invocation_score']:.2f} | "
                f"{row['tool_usage_score']:.2f} | "
                f"{tasks_count} |"
            )
        lines.append("")
        paths.leaderboard_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_baseline_details(
        self,
        paths: ReportPaths,
        score_results: list[BenchmarkScoreResult],
        run_results: list[BenchmarkRunResult],
    ) -> None:
        runs_by_key = {(item.task_id, item.baseline): item for item in run_results}
        per_baseline: dict[str, list[BenchmarkScoreResult]] = {}
        for item in score_results:
            per_baseline.setdefault(item.baseline, []).append(item)

        for baseline_name, items in per_baseline.items():
            target = paths.baselines_dir / f"{baseline_name}.md"
            lines = [
                f"# Baseline detail: {baseline_name}",
                "",
                f"共评估 {len(items)} 个任务。详细的扣分原因摘自 `metric_details`，方便回归调试。",
                "",
            ]
            for item in items:
                run_result = runs_by_key.get((item.task_id, baseline_name))
                lines.extend(self._render_task_detail(item, run_result))
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _render_task_detail(
        self,
        score: BenchmarkScoreResult,
        run: BenchmarkRunResult | None,
    ) -> list[str]:
        scores = score.scores
        details = score.metric_details or {}
        lines = [
            f"## {score.task_id} ({score.difficulty}) — total={scores.total_score:.2f}",
            "",
            (
                f"- Hard={scores.hard_constraint_score:.2f}, "
                f"State={scores.state_tracking_score:.2f} "
                f"(profile={scores.profile_tracking_score:.2f}, "
                f"session={scores.session_tracking_score:.2f}), "
                f"Goal={scores.goal_alignment_score:.2f}, "
                f"Replan={scores.replanning_score:.2f}, "
                f"Know={scores.knowledge_coverage_score:.2f}, "
                f"RAGUse={scores.rag_invocation_score:.2f}, "
                f"ToolUse={scores.tool_usage_score:.2f}"
            ),
        ]

        hard_details = details.get("hard_constraint", {})
        forbidden_hits = hard_details.get("forbidden_hits", []) if isinstance(hard_details, dict) else []
        if forbidden_hits:
            lines.append(f"- ⚠️ 踩到的禁词：{forbidden_hits}")

        replanning_details = details.get("replanning", {})
        if isinstance(replanning_details, dict) and replanning_details.get("applicable"):
            lines.append(
                f"- replanning: answer_changed={replanning_details.get('answer_changed')}, "
                f"required_tools={replanning_details.get('required_tools', [])}, "
                f"used_tools_after_first_turn={replanning_details.get('used_tools_after_first_turn', [])}"
            )

        tool_usage_details = details.get("tool_usage", {})
        if isinstance(tool_usage_details, dict) and tool_usage_details.get("applicable"):
            lines.append(
                f"- tool_usage: required_any_of={tool_usage_details.get('required_any_of', [])}, "
                f"matched={tool_usage_details.get('matched', [])}, "
                f"actually_invoked={tool_usage_details.get('actually_invoked', [])}"
            )

        knowledge_details = details.get("knowledge_coverage", {})
        if isinstance(knowledge_details, dict) and knowledge_details.get("applicable"):
            lines.append(
                f"- knowledge_coverage: hits={knowledge_details.get('knowledge_point_hits', [])}, "
                f"total={knowledge_details.get('knowledge_points_total', [])}"
            )

        if details.get("total"):
            breakdown = details["total"].get("breakdown", []) if isinstance(details["total"], dict) else []
            if breakdown:
                pieces = [
                    f"{entry['metric']}={entry['score']:.2f}*{entry['weight']:.2f}"
                    for entry in breakdown
                ]
                lines.append("- weighted total breakdown: " + ", ".join(pieces))

        if run is not None:
            answer_preview = ""
            if run.turn_results:
                answer_preview = " ".join(run.turn_results[-1].answer.split())[:240]
            if answer_preview:
                lines.append(f"- final answer preview: {answer_preview}")

        if score.metric_details.get("runner_error"):
            lines.append(f"- runner_error: {score.metric_details['runner_error']}")

        lines.append("")
        return lines

    def _build_failure_result(
        self,
        task: BenchmarkTask,
        baseline_name: str,
        user_id: str,
        error: Exception,
    ) -> tuple[BenchmarkRunResult, BenchmarkScoreResult]:
        """Create a failure-shaped run and score result without aborting the whole suite."""
        run_result = BenchmarkRunResult(
            task_id=task.task_id,
            baseline=baseline_name,
            user_id=user_id,
            finished_at=datetime.utcnow().isoformat(),
            final_state={"error": str(error)},
        )
        score_result = BenchmarkScoreResult(
            task_id=task.task_id,
            baseline=baseline_name,
            difficulty=task.difficulty,
            scores=MetricScores(),
            metric_details={"runner_error": str(error)},
            optional_judge={},
        )
        return run_result, score_result
