"""Shared baseline that runs a `WellnessPlanningAgent` with a tool-group filter."""

from __future__ import annotations

from typing import Any, Iterable

from wellnessagent import WellnessPlanningAgent

from ..schemas import BenchmarkStep, BenchmarkTurnResult
from .base import BaselineAdapter


class WellnessAgentBaseline(BaselineAdapter):
    """ReAct-agent backed baseline whose toolset is determined by `tool_groups`.

    Subclasses pick which capability groups are exposed to the LLM. The agent
    is always instantiated with both memory and RAG infrastructure so that
    `seed_knowledge_base` and `clear_user_memories` keep working as
    plumbing-level helpers, but only the registered tools are visible to the
    LLM at planning time.
    """

    name: str = "wellness_agent"
    tool_groups: tuple[str, ...] = ("memory", "rag")

    def __init__(self, user_id: str):
        super().__init__(user_id=user_id)
        self.agent: WellnessPlanningAgent | None = None

    @property
    def _enabled_groups(self) -> tuple[str, ...]:
        return tuple(group for group in self.tool_groups if group)

    def setup(self) -> None:
        self.agent = WellnessPlanningAgent(
            user_id=self.user_id,
            tool_groups=self._enabled_groups,
        )

    def reset(self) -> None:
        if self.agent is None:
            self.setup()
        assert self.agent is not None
        self.agent.clear_user_memories()
        if "rag" not in self._enabled_groups:
            return
        try:
            self.agent.rag_tool.run(
                {
                    "action": "clear",
                    "confirm": True,
                    "namespace": self.agent.rag_tool.rag_namespace,
                }
            )
        except Exception:
            pass

    def seed_knowledge_base(self) -> list[str]:
        """Seed the knowledge base only when the baseline actually needs RAG."""
        assert self.agent is not None
        if "rag" not in self._enabled_groups:
            return []
        return self.agent.seed_knowledge_base()

    def run_turn(self, turn_index: int, user_message: str) -> BenchmarkTurnResult:
        assert self.agent is not None
        result = self.agent.chat_with_trace(user_message)
        steps = [BenchmarkStep.model_validate(step) for step in result.get("steps", [])]
        return BenchmarkTurnResult(
            turn_index=turn_index,
            user_message=user_message,
            answer=result.get("answer", ""),
            terminated_reason=result.get("terminated_reason", "unknown"),
            steps=steps,
            state=result.get("state", {}),
            metadata={
                "baseline": self.name,
                "tool_groups": list(self._enabled_groups),
            },
        )

    def get_final_state(self) -> dict[str, Any]:
        assert self.agent is not None
        state = self.agent.get_state_dict()
        state["baseline_tool_groups"] = list(self._enabled_groups)
        return state

    def teardown(self) -> None:
        if self.agent is not None:
            self.agent.cleanup()


def make_wellness_baseline(name: str, groups: Iterable[str]) -> type[WellnessAgentBaseline]:
    """Helper to build small subclasses without boilerplate."""
    enabled = tuple(groups)

    class _Adapter(WellnessAgentBaseline):
        pass

    _Adapter.name = name
    _Adapter.tool_groups = enabled
    _Adapter.__name__ = f"{name.title().replace('_', '')}Baseline"
    return _Adapter
