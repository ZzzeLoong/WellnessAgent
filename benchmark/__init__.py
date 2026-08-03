"""Benchmark package for WellnessAgent（惰性加载）。

改为惰性导出（与 ``memory/types``、``protocols``、``wellnessagent`` 同款范式）：
顶层 ``from .runner import BenchmarkRunner`` 会连带拉起 ``baselines`` →
``WellnessPlanningAgent`` 整条重依赖链（LLM / 记忆 / 向量库）。这让
「只想用 evaluator 算个分」的场景（单测、A/B 复算落盘分数）必须先满足全部
运行时依赖，非常别扭。惰性化后：

    from benchmark.evaluators.orchestration import score_constraint_adherence  # 零重依赖
    from benchmark import BenchmarkRunner                                      # 需要时才拉链

对外符号与行为保持不变。
"""

_lazy_map = {
    "BenchmarkRunner": ".runner:BenchmarkRunner",
    "load_all_tasks": ".task_loader:load_all_tasks",
    "load_task_by_id": ".task_loader:load_task_by_id",
}


def __getattr__(name):
    if name in _lazy_map:
        module_path, attr = _lazy_map[name].rsplit(":", 1)
        import importlib

        mod = importlib.import_module(module_path, __package__)
        obj = getattr(mod, attr)
        globals()[name] = obj
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["BenchmarkRunner", "load_all_tasks", "load_task_by_id"]
