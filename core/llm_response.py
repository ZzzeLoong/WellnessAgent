"""Function Calling 响应数据结构（WellnessAgent 自研）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ToolCall:
    """一次工具调用意图。``arguments`` 为 JSON 字符串。"""

    id: str
    name: str
    arguments: str


@dataclass
class LLMToolResponse:
    """带原生 tool_calls 的模型响应。"""

    content: Optional[str]
    tool_calls: List[ToolCall]
    model: str
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0

