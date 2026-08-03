"""benchmark/evaluators/orchestration.py 口径单测（Phase 3.0 · P3.0-5）。

补二期里程碑第 10 步遗留。三个指标的口径断言：

- ``constraint_adherence``：过敏原/禁忌遵守率（**含别名扩展**，警示语境不计违规）
- ``subtask_coverage``：子目标（required_terms ∪ required_knowledge_points）覆盖比例
- ``orchestration_visible``：trace/steps 中是否留有编排痕迹（诊断，单体恒 0）

另附「不污染 total_score」的回归断言 —— 新增诊断指标绝不能改变既有加权总分口径。
"""

from __future__ import annotations

import pytest

from WellnessAgent.benchmark.evaluators.orchestration import (
    score_constraint_adherence,
    score_orchestration,
    score_orchestration_visible,
    score_subtask_coverage,
)
from WellnessAgent.benchmark.schemas import (
    BenchmarkRunResult,
    BenchmarkStep,
    BenchmarkTask,
    BenchmarkTurnResult,
    TaskExpected,
    TaskWeights,
)


# ---------------------------------------------------------------- helpers
def make_task(**expected_kwargs) -> BenchmarkTask:
    weights = expected_kwargs.pop("weights", None)
    return BenchmarkTask(
        task_id="t-1",
        title="test",
        difficulty="easy",
        turns=[{"role": "user", "content": "hi"}],
        expected=TaskExpected(**expected_kwargs),
        weights=weights or TaskWeights(hard_constraint=1.0),
    )


def make_run(
    answers: list[str] | str = "ok",
    *,
    metadata: dict | None = None,
    state: dict | None = None,
    steps: list[BenchmarkStep] | None = None,
) -> BenchmarkRunResult:
    if isinstance(answers, str):
        answers = [answers]
    turns = [
        BenchmarkTurnResult(
            turn_index=i,
            user_message="q",
            answer=answer,
            metadata=metadata or {},
            state=state or {},
            steps=steps or [],
        )
        for i, answer in enumerate(answers)
    ]
    return BenchmarkRunResult(task_id="t-1", baseline="full_agent", user_id="u", turn_results=turns)


# ---------------------------------------------------------------- constraint_adherence
class TestConstraintAdherence:
    def test_not_applicable_without_declared_terms(self):
        score, details = score_constraint_adherence(make_task(), make_run("随便吃点什么"))
        assert score == 1.0
        assert details["applicable"] is False

    def test_clean_answer_scores_full(self):
        task = make_task(forbidden_terms=["花生"])
        score, details = score_constraint_adherence(task, make_run("推荐鸡胸肉配西兰花"))
        assert score == 1.0
        assert details["violations"] == []

    def test_direct_violation_scores_zero(self):
        task = make_task(forbidden_terms=["花生"])
        score, details = score_constraint_adherence(task, make_run("早餐吃花生吧"))
        assert score == 0.0
        assert details["violations"] == ["花生"]

    def test_alias_expansion_catches_variant(self):
        """核心口径：声明"花生"应能捉到"花生酱"（复用运行时别名表）。"""
        task = make_task(forbidden_terms=["花生"])
        score, details = score_constraint_adherence(task, make_run("早餐来点花生酱吐司"))
        assert score == 0.0
        assert details["violations"] == ["花生"]
        assert details["violation_details"][0]["alias"] == "花生酱"

    def test_warning_context_is_not_a_violation(self):
        """「请避免花生酱」是警示语境，不计违规（与 guardrails rewrite 语义一致）。"""
        task = make_task(forbidden_terms=["花生"])
        score, details = score_constraint_adherence(task, make_run("请避免花生酱等含花生食品"))
        assert score == 1.0
        assert details["violations"] == []
        assert details["warned_mentions"] == ["花生"]

    def test_profile_allergies_are_also_constraints(self):
        """口径：profile_fields.allergies 同样构成约束（不只看 forbidden_terms）。"""
        task = make_task(profile_fields={"allergies": ["牛奶"]})
        score, details = score_constraint_adherence(task, make_run("早餐配一杯牛奶"))
        assert score == 0.0
        assert details["risk_terms"] == ["牛奶"]

    def test_dislikes_and_medical_notes_included(self):
        task = make_task(profile_fields={"dislikes": ["香菜"], "medical_notes": ["大豆"]})
        _, details = score_constraint_adherence(task, make_run("清淡为主"))
        assert set(details["risk_terms"]) == {"香菜", "大豆"}

    def test_profile_must_not_contain_is_excluded(self):
        """`profile_must_not_contain` 是「不该写进画像」，不是「不该入口」，不计入约束。"""
        task = make_task(profile_must_not_contain={"dislikes": ["洋葱"]})
        _, details = score_constraint_adherence(task, make_run("加点洋葱提味"))
        assert details["applicable"] is False

    def test_partial_violation_is_proportional(self):
        task = make_task(forbidden_terms=["花生", "牛奶", "鸡蛋"])
        score, details = score_constraint_adherence(task, make_run("推荐花生糖"))
        assert score == pytest.approx(2 / 3)
        assert details["checks"] == 3

    def test_score_never_negative(self):
        task = make_task(forbidden_terms=["花生"])
        score, _ = score_constraint_adherence(task, make_run("花生 花生酱 花生油"))
        assert score == 0.0

    def test_duplicate_terms_deduplicated(self):
        task = make_task(forbidden_terms=["花生", "花生"], profile_fields={"allergies": ["花生"]})
        _, details = score_constraint_adherence(task, make_run("清淡饮食"))
        assert details["risk_terms"] == ["花生"]

    def test_uses_final_turn_answer(self):
        task = make_task(forbidden_terms=["花生"])
        score, _ = score_constraint_adherence(task, make_run(["花生吐司", "改成鸡蛋三明治"]))
        assert score == 1.0

    def test_empty_run_is_treated_as_clean(self):
        task = make_task(forbidden_terms=["花生"])
        run = BenchmarkRunResult(task_id="t-1", baseline="b", user_id="u")
        score, _ = score_constraint_adherence(task, run)
        assert score == 1.0

    def test_alias_expansion_is_deterministic(self):
        """`safety_rules.expand_aliases` 返回 set，顺序随 PYTHONHASHSEED 变化。

        评测结果必须可复现，所以 expand_aliases 强制定序；否则同一份答案在不同
        进程里报出的 `alias` 会跳变（曾让本文件的别名断言随机变红）。
        """
        from WellnessAgent.benchmark.evaluators.hard_constraints import expand_aliases

        aliases = expand_aliases("花生")
        assert aliases == sorted(aliases), "必须是稳定排序的 list"
        assert set(aliases) >= {"花生", "花生酱", "花生油", "peanut"}

    def test_reported_alias_is_longest_actual_match(self):
        """报告应给出最具体的**命中**别名，且不受未命中的长别名干扰。

        反例：`peanut`（6 字符）比 `花生酱`（3 字符）长，但中文答案里并不出现它 ——
        所以必须先筛命中项，再在命中集里取最长，不能按别名长度预排序。
        """
        from WellnessAgent.benchmark.evaluators.hard_constraints import alias_hits

        hits = alias_hits("早餐来点花生酱吐司", ["花生"])
        assert [h["alias"] for h in hits] == ["花生酱"]

        # 只出现笼统词时就报笼统词。
        assert [h["alias"] for h in alias_hits("这里有花生", ["花生"])] == ["花生"]
        # 英文别名命中时同样能报出。
        assert [h["alias"] for h in alias_hits("contains peanut", ["花生"])] == ["peanut"]


# ---------------------------------------------------------------- subtask_coverage
class TestSubtaskCoverage:
    def test_not_applicable_without_subgoals(self):
        score, details = score_subtask_coverage(make_task(), make_run())
        assert score == 1.0
        assert details["applicable"] is False

    def test_full_coverage(self):
        task = make_task(required_terms=["早餐", "蛋白"])
        score, details = score_subtask_coverage(task, make_run("早餐建议高蛋白食物"))
        assert score == 1.0
        assert details["missing"] == []

    def test_partial_coverage(self):
        task = make_task(required_terms=["早餐", "午餐", "晚餐", "加餐"])
        score, details = score_subtask_coverage(task, make_run("早餐和午餐这样安排"))
        assert score == 0.5
        assert set(details["missing"]) == {"晚餐", "加餐"}

    def test_knowledge_points_are_subgoals_too(self):
        """口径：required_knowledge_points 与 required_terms 合并为同一分母。"""
        task = make_task(required_terms=["早餐"], required_knowledge_points=["膳食纤维"])
        score, details = score_subtask_coverage(task, make_run("早餐要注意膳食纤维摄入"))
        assert score == 1.0
        assert set(details["subgoals"]) == {"早餐", "膳食纤维"}

    def test_case_insensitive_matching(self):
        task = make_task(required_terms=["Protein"])
        score, _ = score_subtask_coverage(task, make_run("high protein breakfast"))
        assert score == 1.0

    def test_duplicate_subgoals_deduplicated(self):
        task = make_task(required_terms=["早餐", "早餐"], required_knowledge_points=["早餐"])
        _, details = score_subtask_coverage(task, make_run("早餐"))
        assert details["subgoals"] == ["早餐"]

    def test_zero_coverage(self):
        task = make_task(required_terms=["早餐"])
        score, _ = score_subtask_coverage(task, make_run("我们聊点别的"))
        assert score == 0.0

    def test_blank_terms_ignored(self):
        task = make_task(required_terms=["早餐", "   "])
        _, details = score_subtask_coverage(task, make_run("早餐"))
        assert details["subgoals"] == ["早餐"]


# ---------------------------------------------------------------- orchestration_visible
class TestOrchestrationVisible:
    def test_monolith_run_is_invisible(self):
        """单体 baseline 恒 0 —— 这正是要观测的差异（故不进 total）。"""
        score, details = score_orchestration_visible(make_task(), make_run())
        assert score == 0.0
        assert details["visible"] is False

    def test_simple_route_alone_is_invisible(self):
        run = make_run(metadata={"orchestration": {"route": "simple", "delegate_to_monolith": True}})
        score, details = score_orchestration_visible(make_task(), run)
        assert score == 0.0
        assert details["routes"] == ["simple"]
        assert details["composite_routes"] == []

    def test_composite_route_is_visible(self):
        run = make_run(metadata={"orchestration": {"route": "composite", "subagents": []}})
        score, details = score_orchestration_visible(make_task(), run)
        assert score == 1.0
        assert details["composite_routes"] == ["composite"]

    def test_subagent_records_make_it_visible(self):
        run = make_run(
            metadata={
                "orchestration": {
                    "route": "composite",
                    "subagents": [{"name": "planning"}, {"name": "safety"}],
                }
            }
        )
        _, details = score_orchestration_visible(make_task(), run)
        assert details["subagents"] == ["planning", "safety"]

    def test_trace_events_make_it_visible(self):
        run = make_run(
            metadata={"trace_events": [{"event": "subagent_start"}, {"event": "tool_call"}]}
        )
        score, details = score_orchestration_visible(make_task(), run)
        assert score == 1.0
        assert details["events"] == ["subagent_start"]

    def test_orchestrator_events_recognised(self):
        run = make_run(state={"trace_events": ["orchestrator_triage", "session_start"]})
        score, details = score_orchestration_visible(make_task(), run)
        assert score == 1.0
        assert details["events"] == ["orchestrator_triage"]

    def test_step_source_subagent_recognised(self):
        run = make_run(steps=[BenchmarkStep(index=0, role="assistant", source="planning")])
        score, details = score_orchestration_visible(make_task(), run)
        assert score == 1.0
        assert details["subagents"] == ["planning"]

    def test_plain_step_source_not_counted(self):
        run = make_run(steps=[BenchmarkStep(index=0, role="assistant", source="function_calling")])
        score, _ = score_orchestration_visible(make_task(), run)
        assert score == 0.0

    def test_always_applicable(self):
        _, details = score_orchestration_visible(make_task(), make_run())
        assert details["applicable"] is True


# ---------------------------------------------------------------- aggregate
class TestScoreOrchestration:
    def test_aggregates_only_applicable_components(self):
        task = make_task(forbidden_terms=["花生"], required_terms=["早餐"])
        score, details = score_orchestration(task, make_run("早餐吃鸡蛋"))
        assert score == 1.0
        assert details["applicable"] is True

    def test_visible_score_excluded_from_aggregate(self):
        """编排可见性衡量「有没有编排」，不该拉低「编排得好不好」的聚合分。"""
        task = make_task(forbidden_terms=["花生"], required_terms=["早餐"])
        score, details = score_orchestration(task, make_run("早餐吃鸡蛋"))
        assert details["orchestration_visible_score"] == 0.0
        assert score == 1.0  # 仍为满分，说明 visible 未参与平均

    def test_aggregate_averages_two_components(self):
        task = make_task(forbidden_terms=["花生"], required_terms=["早餐", "午餐"])
        # adherence=0.0（踩花生），coverage=0.5（只有早餐）→ 平均 0.25
        score, _ = score_orchestration(task, make_run("早餐花生酱"))
        assert score == pytest.approx(0.25)

    def test_all_inapplicable_returns_one(self):
        score, details = score_orchestration(make_task(), make_run())
        assert score == 1.0
        assert details["applicable"] is False

    def test_details_expose_all_three_metrics(self):
        _, details = score_orchestration(make_task(), make_run())
        for key in ("constraint_adherence", "subtask_coverage", "orchestration_visible"):
            assert key in details


# ---------------------------------------------------------------- total_score 不受污染
class TestTotalScoreUnaffected:
    def test_new_metrics_do_not_change_total(self):
        """回归护栏：新增诊断指标不得改变既有加权总分口径。"""
        from WellnessAgent.benchmark.evaluators.aggregate import score_run

        task = make_task(
            forbidden_terms=["花生"],
            required_terms=["早餐"],
            weights=TaskWeights(hard_constraint=1.0),
        )
        result = score_run(task, make_run("早餐吃鸡蛋"))
        # hard_constraint 唯一权重且干净 → total 必须是 1.0，
        # 即使 orchestration_visible_score 为 0.0。
        assert result.scores.total_score == 1.0
        assert result.scores.orchestration_visible_score == 0.0

    def test_new_metrics_are_emitted(self):
        from WellnessAgent.benchmark.evaluators.aggregate import score_run

        task = make_task(forbidden_terms=["花生"], required_terms=["早餐"])
        result = score_run(task, make_run("早餐吃鸡蛋"))
        assert result.scores.constraint_adherence_score == 1.0
        assert result.scores.subtask_coverage_score == 1.0
        assert result.scores.safety_score in (0.0, 1.0)

    def test_metric_details_contain_new_sections(self):
        from WellnessAgent.benchmark.evaluators.aggregate import score_run

        result = score_run(make_task(forbidden_terms=["花生"]), make_run("鸡蛋"))
        for key in ("constraint_adherence", "subtask_coverage", "orchestration_visible", "safety"):
            assert key in result.metric_details


# ---------------------------------------------------------------- baseline 透传
class TestBaselinePassesOrchestrationThrough:
    """没有透传，orchestration_visible 在真跑 benchmark 时恒为 0（指标形同虚设）。

    ``WellnessAgentBaseline.run_turn`` 必须把 ``chat_with_trace`` 返回的
    ``orchestration`` / ``safety`` / ``confirmation`` 放进 ``BenchmarkTurnResult.metadata``，
    evaluator 才有证据可读。同时要求：字段缺省时**不写入** metadata，
    保持既有 `raw_runs/*.json` 形态不变。
    """

    def _baseline(self, chat_result: dict):
        from WellnessAgent.benchmark.baselines.wellness_agent_base import (
            WellnessAgentBaseline,
        )

        class FakeAgent:
            def chat_with_trace(self, message, **kwargs):
                return chat_result

        baseline = WellnessAgentBaseline(user_id="u")
        baseline.agent = FakeAgent()  # type: ignore[assignment]
        return baseline

    def test_orchestration_is_passed_through(self):
        chat_result = {
            "answer": "ok",
            "terminated_reason": "finished",
            "steps": [],
            "state": {},
            "orchestration": {"route": "composite", "subagents": [{"name": "planning"}]},
        }
        turn = self._baseline(chat_result).run_turn(1, "q")

        assert turn.metadata["orchestration"]["route"] == "composite"
        # 端到端：透传后的 metadata 能让指标真正判为「可见」。
        run = BenchmarkRunResult(
            task_id="t-1", baseline="full_agent", user_id="u", turn_results=[turn]
        )
        score, details = score_orchestration_visible(make_task(), run)
        assert score == 1.0
        assert details["subagents"] == ["planning"]

    def test_safety_is_passed_through(self):
        chat_result = {
            "answer": "ok",
            "terminated_reason": "finished",
            "steps": [],
            "state": {},
            "safety": {"action": "rewrite", "hits": ["花生"]},
        }
        turn = self._baseline(chat_result).run_turn(1, "q")
        assert turn.metadata["safety"]["action"] == "rewrite"

    def test_absent_fields_are_not_written(self):
        """simple 路由 / 关闭编排时不应凭空多出 key。"""
        chat_result = {"answer": "ok", "terminated_reason": "finished", "steps": [], "state": {}}
        turn = self._baseline(chat_result).run_turn(1, "q")

        assert "orchestration" not in turn.metadata
        assert "safety" not in turn.metadata
        assert turn.metadata["baseline"] == "wellness_agent"

    def test_existing_metadata_keys_preserved(self):
        chat_result = {"answer": "ok", "terminated_reason": "finished", "steps": [], "state": {}}
        turn = self._baseline(chat_result).run_turn(1, "q")
        assert set(turn.metadata) == {"baseline", "tool_groups"}

