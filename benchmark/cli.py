"""CLI entrypoint for the standalone benchmark package."""

from __future__ import annotations

import argparse
from typing import Sequence

from .baselines import ALL_BASELINES
from .runner import BenchmarkRunner
from .subsets import select_tasks_by_limit
from .task_loader import load_all_tasks, load_task_by_id


def build_parser() -> argparse.ArgumentParser:
    """Build benchmark CLI parser."""
    parser = argparse.ArgumentParser(description="Run WellnessAgent benchmark tasks.")
    parser.add_argument(
        "--baseline",
        action="append",
        choices=list(ALL_BASELINES),
        help=(
            "运行的 baseline，可重复指定，例如 --baseline llm_only --baseline full_agent。"
            f"默认运行全部：{', '.join(ALL_BASELINES)}。"
        ),
    )
    parser.add_argument(
        "--task",
        action="append",
        help="按 task_id 运行单个/多个任务，可重复指定，例如 --task task_01 --task task_05。",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="运行全部 20 个任务。等价于 --limit 20。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        choices=[1, 10, 20],
        help="快速选择运行子集：1=最小烟雾测试，10=精选评测集，20=全部。",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="关闭单任务进度日志。",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="完全静默运行，仅输出错误。",
    )
    return parser


def _resolve_baselines(arg_baselines: Sequence[str] | None) -> list[str]:
    """Default to all baselines if none specified."""
    if not arg_baselines:
        return list(ALL_BASELINES)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in arg_baselines:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _resolve_tasks(args: argparse.Namespace):
    """Load and filter tasks based on CLI flags."""
    if args.task:
        return [load_task_by_id(task_id) for task_id in args.task]

    all_tasks = load_all_tasks()
    if args.all:
        return all_tasks

    if args.limit is not None:
        return select_tasks_by_limit(all_tasks, args.limit)

    return all_tasks


def main() -> None:
    """Execute benchmark from the command line."""
    parser = build_parser()
    args = parser.parse_args()

    baselines = _resolve_baselines(args.baseline)
    tasks = _resolve_tasks(args)
    if not tasks:
        print("[benchmark] 未匹配到任何任务，已退出。")
        return

    runner = BenchmarkRunner(
        verbose=not args.quiet,
        show_progress=(not args.quiet) and (not args.no_progress),
    )
    if not args.quiet:
        print(
            f"[benchmark] 计划运行 {len(tasks)} 个任务 x {len(baselines)} 个 baseline = "
            f"{len(tasks) * len(baselines)} 次评测。"
        )
        print(f"[benchmark] baselines = {', '.join(baselines)}")
        print(f"[benchmark] tasks     = {', '.join(task.task_id for task in tasks)}")
    runner.run(tasks=tasks, baselines=baselines)


if __name__ == "__main__":
    main()
