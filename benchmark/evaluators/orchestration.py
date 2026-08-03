"""Orchestration-quality scoring（Phase 3.0 · P3.0-5，补二期里程碑第 10 步）。

回答的问题是：**编排层到底带来了什么**。二期加了 Orchestrator + 4 个 SubAgent，
但没有任何量化口径能证明"编排比单体更守约束"，于是本模块给出三个指标：

| 指标 | 口径 |
|------|------|
| ``constraint_adherence`` | 过敏原/预算/时间约束遵守率。复用运行时 ``safety_rules`` 的别名表做命中检测，再排除"警示语境"命中 |
| ``subtask_coverage``     | 任务声明的子目标（``required_terms`` + ``required_knowledge_points``）在最终答案中的覆盖比例 |
| ``orchestration_visible``| trace/steps 中是否存在 ``orchestrator_*`` / ``subagent_*`` 痕迹（编排可见性；单体 baseline 恒 0，属**诊断**指标） |

三条设计原则（与既有 evaluator 保持一致）：

1. **只读已落盘字段**：仅消费 ``BenchmarkRunResult``（answer / steps / state / metadata），
   不重跑 agent、不调 LLM → 结果可复现。
2. **不可评时返回 1.0 + ``applicable=False``**：任务没声明约束就不该拉低均值。
3. **不进 ``total_score``**：``orchestration_visible`` 对 ``llm_only`` 天然不可达，
   三个指标统一作为诊断输出，避免结构性惩罚无编排的 baseline（沿用 ``tool_usage`` 的处理）。
"""

from __future__ import annotations

from typing import Any

from ..schemas import BenchmarkRunResult, BenchmarkTask
from .hard_constraints import alias_hits, collect_task_risk_terms


# 编排可见性信号：trace 事件名前缀 / step 里出现的编排痕迹。
ORCHESTRATION_EVENT_PREFIXES: tuple[str, ...] = ("orchestrator_", "subagent_")

# 二期 SubAgent 名称（用于在 metadata/state 中识别编排痕迹）。
KNOWN_SUBAGENTS: frozenset[str] = frozenset({"profile", "retrieval", "planning", "safety"})


def _final_answer(run: BenchmarkRunResult) -> str:
    return (run.turn_results[-1].answer if run.turn_results else "") or ""


def score_constraint_adherence(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Score whether the answer respects every risk constraint the task declares.

    与 ``hard_constraint_score`` 的分工：

    - ``hard_constraint_score``：只看 ``forbidden_terms`` 的**精确子串**，是硬红线（进 total）。
    - 本指标：额外覆盖 ``profile_fields`` 里的 allergies/dislikes/medical_notes，
      并做**别名扩展**（"花生" 也能捉到 "花生酱"），是编排质量的**细粒度诊断**。

    评分：``1 - 违规词数 / 声明词数``。"命中但处于警示语境"（如"请避免花生酱"）
    **不计违规** —— 与运行时 guardrails 的 rewrite 语义一致。
    """
    risk_terms = collect_task_risk_terms(task)
    if not risk_terms:
        return 1.0, {"applicable": False, "risk_terms": []}

    answer = _final_answer(run)
    hits = alias_hits(answer, risk_terms)
    violations = [hit for hit in hits if not hit["safe_context"]]
    warned = [hit for hit in hits if hit["safe_context"]]

    score = max(0.0, 1.0 - len(violations) / len(risk_terms))
    return score, {
        "applicable": True,
        "risk_terms": risk_terms,
        "violations": [hit["term"] for hit in violations],
        "violation_details": violations,
        "warned_mentions": [hit["term"] for hit in warned],
        "checks": len(risk_terms),
    }


def score_subtask_coverage(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Score how many declared subgoals show up in the final answer.

    子目标口径 = ``required_terms`` ∪ ``required_knowledge_points``。前者是"用户要什么"，
    后者是"必须落地哪些知识点"，二者合起来才是"这次编排要覆盖的子任务集合"。

    与 ``goal_alignment_score``（只看 ``required_terms``）刻意重叠但不相同：编排的价值
    正在于"多个子代理各自负责一块，合起来覆盖更全"，因此把知识点也纳入同一分母。
    """
    subgoals: list[str] = []
    for term in list(task.expected.required_terms) + list(task.expected.required_knowledge_points):
        text = str(term).strip()
        if text and text not in subgoals:
            subgoals.append(text)

    if not subgoals:
        return 1.0, {"applicable": False, "subgoals": []}

    answer = _final_answer(run).lower()
    hits = [goal for goal in subgoals if goal.lower() in answer]
    missing = [goal for goal in subgoals if goal not in hits]
    return len(hits) / len(subgoals), {
        "applicable": True,
        "subgoals": subgoals,
        "hits": hits,
        "missing": missing,
        "checks": len(subgoals),
    }


def _collect_orchestration_signals(run: BenchmarkRunResult) -> dict[str, list[str]]:
    """Collect orchestration traces from the run's already-persisted fields.

    三个来源（都不需要额外落盘）：

    1. ``turn.metadata['orchestration']``：``chat_with_trace`` 的对外字段（route/subagents）；
    2. ``turn.metadata['trace_events']`` 或 ``turn.state['trace_events']``：若 baseline
       把 trace 事件名带了进来，则识别 ``orchestrator_*`` / ``subagent_*``；
    3. ``step.source``：SubAgent 执行的步骤会带上子代理来源标记。
    """
    routes: list[str] = []
    subagents: list[str] = []
    events: list[str] = []

    def _scan_events(raw: Any) -> None:
        if not isinstance(raw, (list, tuple)):
            return
        for item in raw:
            name = item.get("event") if isinstance(item, dict) else item
            text = str(name or "")
            if text.startswith(ORCHESTRATION_EVENT_PREFIXES) and text not in events:
                events.append(text)

    for turn in run.turn_results:
        meta = turn.metadata or {}
        orchestration = meta.get("orchestration")
        if isinstance(orchestration, dict):
            route = orchestration.get("route")
            if route and str(route) not in routes:
                routes.append(str(route))
            for entry in orchestration.get("subagents") or []:
                name = entry.get("name") if isinstance(entry, dict) else entry
                if name and str(name) not in subagents:
                    subagents.append(str(name))
        _scan_events(meta.get("trace_events"))
        _scan_events((turn.state or {}).get("trace_events"))
        for step in turn.steps:
            source = str(step.source or "")
            if not source:
                continue
            if source.startswith(ORCHESTRATION_EVENT_PREFIXES) and source not in events:
                events.append(source)
            elif source in KNOWN_SUBAGENTS and source not in subagents:
                subagents.append(source)

    return {"routes": routes, "subagents": subagents, "events": events}


def score_orchestration_visible(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Score whether orchestration actually happened and left evidence.

    **诊断性指标**：单体路径（``route=simple``）或无编排的 baseline 恒为 0，这正是
    我们要观测的差异，因此**绝不能**进 ``total_score``。

    判定为「可见」需要至少一条证据：composite 路由 / 有子代理记录 / 有 ``orchestrator_*``
    或 ``subagent_*`` 事件。
    """
    signals = _collect_orchestration_signals(run)
    composite_routes = [route for route in signals["routes"] if route != "simple"]
    visible = bool(composite_routes or signals["subagents"] or signals["events"])
    return (1.0 if visible else 0.0), {
        "applicable": True,
        "visible": visible,
        "routes": signals["routes"],
        "composite_routes": composite_routes,
        "subagents": signals["subagents"],
        "events": signals["events"],
    }


def score_orchestration(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
) -> tuple[float, dict[str, Any]]:
    """Aggregate the three orchestration metrics into one diagnostic score.

    聚合口径：只平均**可评（applicable）**的分项；``orchestration_visible`` 虽然恒为
    applicable，但它衡量的是"有没有编排"而非"编排得好不好"，故**不进聚合分**，
    只在 details 里单独呈现。全部不可评时返回 1.0（与其他 evaluator 一致）。
    """
    adherence_score, adherence_details = score_constraint_adherence(task, run)
    coverage_score, coverage_details = score_subtask_coverage(task, run)
    visible_score, visible_details = score_orchestration_visible(task, run)

    components: list[float] = []
    if adherence_details.get("applicable"):
        components.append(adherence_score)
    if coverage_details.get("applicable"):
        components.append(coverage_score)

    score = sum(components) / len(components) if components else 1.0
    return score, {
        "applicable": bool(components),
        "constraint_adherence": adherence_details,
        "constraint_adherence_score": adherence_score,
        "subtask_coverage": coverage_details,
        "subtask_coverage_score": coverage_score,
        "orchestration_visible": visible_details,
        "orchestration_visible_score": visible_score,
    }


__all__ = [
    "KNOWN_SUBAGENTS",
    "ORCHESTRATION_EVENT_PREFIXES",
    "score_constraint_adherence",
    "score_orchestration",
    "score_orchestration_visible",
    "score_subtask_coverage",
]

