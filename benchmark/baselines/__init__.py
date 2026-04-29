"""Baseline adapters for the benchmark."""

from .base import BaselineAdapter
from .full_agent import FullAgentBaseline
from .llm_memory import LLMMemoryBaseline
from .llm_only import LLMOnlyBaseline
from .llm_rag import LLMRagBaseline
from .wellness_agent_base import WellnessAgentBaseline

ALL_BASELINES = ("llm_only", "llm_memory", "llm_rag", "full_agent")

__all__ = [
    "ALL_BASELINES",
    "BaselineAdapter",
    "FullAgentBaseline",
    "LLMMemoryBaseline",
    "LLMOnlyBaseline",
    "LLMRagBaseline",
    "WellnessAgentBaseline",
]
