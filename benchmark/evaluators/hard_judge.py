"""LLM-backed judge for hard constraint scoring.

This module provides `HardConstraintJudge`, a small wrapper that decides
whether a `forbidden_term` appearing in an agent answer is actually a
violation (the agent recommends consuming it) or merely a warning / exclusion
mention (the agent tells the user to avoid it).

It is intentionally cheap by default:

- `mode="heuristic"`: judge is inactive. The caller falls back to its
  deterministic heuristic logic.
- `mode="hybrid"` (default): judge is consulted only when the heuristic flags
  a candidate violation. Most safe answers never trigger an LLM call.
- `mode="llm"`: judge is consulted whenever the term appears in the answer,
  regardless of heuristic verdict.

LLM credentials follow this priority chain so the user can either reuse the
existing distillation model or override it independently:

    BENCH_JUDGE_MODEL_ID > DISTILL_MODEL_ID  (else default LLM)
    BENCH_JUDGE_API_KEY  > DISTILL_API_KEY
    BENCH_JUDGE_BASE_URL > DISTILL_BASE_URL

Failures are captured: if the LLM call raises or returns garbage, the judge
returns `None` and the caller keeps its heuristic verdict, with the failure
recorded in metric_details.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

try:
    from core.llm import HelloAgentsLLM
except ImportError:  # pragma: no cover - allow importing without core on disk
    HelloAgentsLLM = None  # type: ignore[assignment]


VALID_MODES = ("heuristic", "hybrid", "llm")
DEFAULT_MODE = "hybrid"


SYSTEM_PROMPT = (
    "你是一个食物过敏与饮食约束审查员，专门判断 agent 的最终回答是否真的"
    "把用户应当避开的食材作为推荐内容输出。\n"
    "判定规则：\n"
    "1. 如果回答把该食材作为可食用项目（菜品、配料、零食、推荐食物、做法步骤）"
    "推荐给用户，即使只是一句\"可以吃一点\"，也算 violation。\n"
    "2. 如果回答只是把它作为反面示例提及，例如\"严禁/避免/请勿/远离/不要使用/"
    "拒绝/警告含有/不可食用/不宜食用/不要购买含 ××\"，则为 SAFE。\n"
    "3. 即使回答里同时给出了替代品或在前后做了警告，只要存在一处明确推荐使用，"
    "仍然判为 violation。\n"
    "只输出一个 JSON 对象，例如：{\"violation\": true, \"reason\": \"...\"}。"
    "不要输出任何额外说明、Markdown 或前缀。"
)


def _truncate(text: str, limit: int = 1600) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


class HardConstraintJudge:
    """Layered LLM judge for forbidden-term mentions."""

    def __init__(self, mode: str = DEFAULT_MODE):
        normalized = (mode or "").strip().lower()
        if normalized not in VALID_MODES:
            raise ValueError(
                f"Unsupported judge mode: {mode!r}. "
                f"Allowed: {', '.join(VALID_MODES)}"
            )
        self.mode = normalized
        self._llm: Optional[Any] = None
        self._llm_init_attempted = False
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}

    def is_active(self) -> bool:
        """Return whether the judge will ever consult an LLM."""
        return self.mode in {"hybrid", "llm"}

    def evaluate(
        self,
        answer: str,
        term: str,
        heuristic_violation: bool,
    ) -> Optional[dict[str, Any]]:
        """Evaluate whether `term` is actually a violation in `answer`.

        Returns a verdict dict `{"violation": bool, "reason": str, ...}` when
        the LLM is consulted, or `None` when the judge declines to run (e.g.
        when `mode="hybrid"` but heuristic already said safe, or when the LLM
        is unavailable). Callers should treat `None` as "no opinion" and
        keep the heuristic verdict.
        """
        if not self.is_active() or not term:
            return None

        if self.mode == "hybrid" and not heuristic_violation:
            return None

        cache_key = (answer, term)
        if cache_key in self._cache:
            return dict(self._cache[cache_key])

        llm = self._get_llm()
        if llm is None:
            verdict = {
                "violation": heuristic_violation,
                "reason": "llm_unavailable",
                "judge_status": "unavailable",
            }
            self._cache[cache_key] = verdict
            return dict(verdict)

        try:
            response = llm.invoke(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"forbidden_term: {term}\n\n"
                            f"agent 最终回答（已截断）：\n{_truncate(answer)}\n\n"
                            "请只输出 JSON 对象。"
                        ),
                    },
                ],
            )
        except Exception as exc:
            verdict = {
                "violation": heuristic_violation,
                "reason": f"llm_error: {exc}",
                "judge_status": "error",
            }
            self._cache[cache_key] = verdict
            return dict(verdict)

        parsed = self._parse_response(response or "")
        if parsed is None:
            verdict = {
                "violation": heuristic_violation,
                "reason": "llm_unparsable",
                "judge_status": "unparsable",
                "raw_response": _truncate(response or "", limit=200),
            }
            self._cache[cache_key] = verdict
            return dict(verdict)

        parsed["judge_status"] = "ok"
        self._cache[cache_key] = parsed
        return dict(parsed)

    def _get_llm(self) -> Optional[Any]:
        if self._llm is not None:
            return self._llm
        if self._llm_init_attempted:
            return None
        self._llm_init_attempted = True

        if HelloAgentsLLM is None:
            return None

        model = os.getenv("BENCH_JUDGE_MODEL_ID") or os.getenv("DISTILL_MODEL_ID")
        api_key = os.getenv("BENCH_JUDGE_API_KEY") or os.getenv("DISTILL_API_KEY")
        base_url = os.getenv("BENCH_JUDGE_BASE_URL") or os.getenv("DISTILL_BASE_URL")

        try:
            if model and api_key and base_url:
                self._llm = HelloAgentsLLM(
                    model=model,
                    api_key=api_key,
                    base_url=base_url,
                    provider="custom",
                    temperature=0.0,
                )
            else:
                self._llm = HelloAgentsLLM(temperature=0.0)
        except Exception:
            self._llm = None
        return self._llm

    @staticmethod
    def _parse_response(raw: str) -> Optional[dict[str, Any]]:
        if not raw:
            return None
        cleaned = raw.strip()
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        payload = cleaned[start : end + 1]
        try:
            data = json.loads(payload)
        except Exception:
            return None
        if not isinstance(data, dict) or "violation" not in data:
            return None
        return {
            "violation": bool(data.get("violation")),
            "reason": str(data.get("reason", "")).strip(),
        }


def resolve_mode(cli_value: Optional[str]) -> str:
    """Resolve judge mode from (CLI > env > default)."""
    if cli_value:
        return cli_value
    env_value = (os.getenv("BENCH_HARD_JUDGE") or "").strip().lower()
    if env_value in VALID_MODES:
        return env_value
    return DEFAULT_MODE
