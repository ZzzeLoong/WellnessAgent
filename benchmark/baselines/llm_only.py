"""LLM-only baseline without tool, memory, or RAG support."""

from __future__ import annotations

from typing import Any

from core.llm import HelloAgentsLLM

from ..schemas import BenchmarkTurnResult
from .base import BaselineAdapter


LLM_ONLY_SYSTEM_PROMPT = """你是一名饮食规划助手。

你需要直接基于多轮对话历史回答用户，不允许调用任何工具，也不拥有显式记忆系统或知识库。
请尽力遵守用户已经提到的约束，并给出自然、完整的中文回答。
"""


class LLMOnlyBaseline(BaselineAdapter):
    """Simple baseline that only relies on the raw LLM."""

    name = "llm_only"

    def __init__(self, user_id: str):
        super().__init__(user_id=user_id)
        self.llm: HelloAgentsLLM | None = None
        self.history: list[dict[str, str]] = []
        self.last_answer: str = ""

    def setup(self) -> None:
        self.llm = HelloAgentsLLM()
        self.history = []
        self.last_answer = ""

    def reset(self) -> None:
        self.history = []
        self.last_answer = ""

    def seed_knowledge_base(self) -> list[str]:
        return []

    def run_turn(self, turn_index: int, user_message: str) -> BenchmarkTurnResult:
        assert self.llm is not None
        self.history.append({"role": "user", "content": user_message})
        messages = [{"role": "system", "content": LLM_ONLY_SYSTEM_PROMPT}]
        messages.extend(self.history)
        answer = self.llm.invoke(messages, temperature=0.2)
        self.last_answer = answer or ""
        self.history.append({"role": "assistant", "content": self.last_answer})
        return BenchmarkTurnResult(
            turn_index=turn_index,
            user_message=user_message,
            answer=self.last_answer,
            terminated_reason="finished",
            steps=[],
            state={
                "current_profile": {},
                "current_profile_summary": "",
                "working_memory_summary": "",
                "distilled_memory_summary": "",
                "rag_summary": "disabled",
                "recent_dialogue_window": self._history_summary(),
            },
            metadata={"baseline": self.name},
        )

    def get_final_state(self) -> dict[str, Any]:
        return {
            "current_profile": {},
            "current_profile_summary": "",
            "working_memory_summary": "",
            "distilled_memory_summary": "",
            "rag_summary": "disabled",
            "recent_dialogue_window": self._history_summary(),
            "last_answer": self.last_answer,
        }

    def teardown(self) -> None:
        return None

    def _history_summary(self) -> str:
        lines: list[str] = []
        for item in self.history[-6:]:
            role_name = "用户" if item["role"] == "user" else "助手"
            lines.append(f"- {role_name}: {' '.join(item['content'].split())}")
        return "\n".join(lines)
