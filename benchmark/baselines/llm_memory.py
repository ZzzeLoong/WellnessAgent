"""LLM + memory baseline (no RAG tools, no knowledge base injection)."""

from __future__ import annotations

from .wellness_agent_base import WellnessAgentBaseline


class LLMMemoryBaseline(WellnessAgentBaseline):
    """Agent that retains profile / session / long-term memory but no RAG tools."""

    name = "llm_memory"
    tool_groups = ("memory",)
