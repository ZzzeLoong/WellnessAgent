"""LLM + RAG baseline (no profile/session/long-term memory tools)."""

from __future__ import annotations

from .wellness_agent_base import WellnessAgentBaseline


class LLMRagBaseline(WellnessAgentBaseline):
    """Agent that uses only RAG knowledge tools, with no persistent memory."""

    name = "llm_rag"
    tool_groups = ("rag",)
