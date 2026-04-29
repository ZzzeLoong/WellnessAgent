"""Full-agent baseline that exposes both memory and RAG tools."""

from __future__ import annotations

from .wellness_agent_base import WellnessAgentBaseline


class FullAgentBaseline(WellnessAgentBaseline):
    """Benchmark adapter for the unrestricted production wellness agent."""

    name = "full_agent"
    tool_groups = ("memory", "rag")
