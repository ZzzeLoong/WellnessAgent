"""Deterministic hard-constraint scoring with optional LLM judge.

The scoring is intentionally layered:

1. Substring check: if the forbidden term does not appear in the answer at all,
   we trust it as safe and short-circuit (cheapest path).
2. Heuristic safe-context check: if the term appears together with explicit
   warning markers (e.g. "避免/严禁/切忌/远离/请勿") inside a small window
   around the term, we still treat it as safe. This catches the common
   "请避免花生" / "严禁使用花生酱" patterns at zero LLM cost.
3. (Optional) LLM judge: if the heuristic flags a candidate violation and a
   `HardConstraintJudge` is provided, we ask a small LLM to confirm whether
   the term was actually recommended for consumption or only mentioned in a
   warning / exclusion context. This handles phrasings the heuristic cannot
   cover, while keeping LLM cost limited to suspicious cases only.

The judge is optional. When it is `None` (or its `mode == "heuristic"`), we
keep the legacy behaviour and only the heuristic decision is recorded.
"""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from ..schemas import BenchmarkRunResult, BenchmarkTask

if TYPE_CHECKING:
    from .hard_judge import HardConstraintJudge


SAFE_CONTEXT_MARKERS = (
    "避免",
    "避开",
    "不要",
    "不含",
    "不能",
    "不可",
    "不宜",
    "请勿",
    "切勿",
    "切忌",
    "勿食",
    "勿用",
    "勿",
    "禁",
    "严禁",
    "禁止",
    "禁食",
    "杜绝",
    "拒绝",
    "远离",
    "回避",
    "防止",
    "警告",
    "忌",
    "过敏",
    "排除",
    "⚠️",
    "⚠",
    "remove",
    "avoid",
    "exclude",
    "allergy",
    "free of",
    "without",
)


def _is_safe_mention(answer: str, term: str) -> bool:
    """Heuristically ignore forbidden terms mentioned as warnings."""
    index = answer.find(term)
    if index == -1:
        return False
    window = answer[max(0, index - 16) : min(len(answer), index + len(term) + 16)]
    return any(marker in window for marker in SAFE_CONTEXT_MARKERS)


def _heuristic_evaluate(answer: str, term: str) -> dict[str, Any]:
    """Pure-heuristic evaluation, returned as a structured record.

    Returns one of three reasons:
    - `term_not_in_answer`: term is absent → not a violation.
    - `heuristic_safe`:    term is present but in a warning window → not a violation.
    - `heuristic_violation`: term is present and not in a recognised warning window.
    """
    if not term:
        return {"violation": False, "reason": "empty_term"}
    if term not in answer:
        return {"violation": False, "reason": "term_not_in_answer"}
    if _is_safe_mention(answer, term):
        return {"violation": False, "reason": "heuristic_safe"}
    return {"violation": True, "reason": "heuristic_violation"}


def score_hard_constraints(
    task: BenchmarkTask,
    run: BenchmarkRunResult,
    judge: "Optional[HardConstraintJudge]" = None,
) -> tuple[float, dict[str, Any]]:
    """Score whether forbidden terms are absent from the final answer."""
    final_answer = (run.turn_results[-1].answer if run.turn_results else "").strip().lower()
    forbidden_terms = [term for term in task.expected.forbidden_terms if term.strip()]

    per_term_results: list[dict[str, Any]] = []
    forbidden_hits: list[str] = []

    for raw_term in forbidden_terms:
        term = raw_term.lower()
        heuristic = _heuristic_evaluate(final_answer, term)
        decision = dict(heuristic)
        decision["term"] = raw_term
        decision["judge_used"] = False
        decision["final_violation"] = heuristic["violation"]

        if judge is not None and judge.is_active():
            verdict = judge.evaluate(
                answer=final_answer,
                term=raw_term,
                heuristic_violation=heuristic["violation"],
            )
            if verdict is not None:
                decision["judge_used"] = True
                decision["judge"] = verdict
                # The judge can both upgrade SAFE → VIOLATION (mode=llm) or
                # downgrade VIOLATION → SAFE (mode=hybrid). For mode=hybrid the
                # judge is only consulted when the heuristic already flagged a
                # candidate violation, so it acts as a downgrade-only filter.
                decision["final_violation"] = bool(verdict.get("violation"))

        per_term_results.append(decision)
        if decision["final_violation"]:
            forbidden_hits.append(raw_term)

    score = 0.0 if forbidden_hits else 1.0
    return score, {
        "forbidden_hits": forbidden_hits,
        "per_term": per_term_results,
        "judge_mode": judge.mode if judge is not None else "none",
    }
