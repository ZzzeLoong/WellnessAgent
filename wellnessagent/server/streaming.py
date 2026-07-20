"""SSE 流式事件（WellnessAgent 自研，R2）。

定义流式对话的事件类型与 SSE 序列化。一期做法（方案 §6.2）：ReAct 循环每步
yield step/tool 事件；最终答案分段 yield ``LLM_CHUNK``。逐 token 输出留待二期。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StreamEventType(str, Enum):
    """流式事件类型。"""

    AGENT_START = "agent_start"
    STEP_START = "step_start"
    THINKING = "thinking"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_FINISH = "tool_call_finish"
    LLM_CHUNK = "llm_chunk"
    SAFETY = "safety"
    AGENT_FINISH = "agent_finish"
    ERROR = "error"
    # 二期 R6/R7 编排 / 子代理 / 确认事件。
    ORCHESTRATOR_TRIAGE = "orchestrator_triage"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_RESULT = "subagent_result"
    ORCHESTRATOR_AGGREGATE = "orchestrator_aggregate"
    CONFIRM = "confirm"


@dataclass
class StreamEvent:
    """一个可序列化为 SSE 的流式事件。"""

    type: StreamEventType
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        """序列化为 ``event: <type>\\ndata: <json>\\n\\n`` 形式的 SSE 帧。"""
        payload = json.dumps(self.data, ensure_ascii=False)
        return f"event: {self.type.value}\ndata: {payload}\n\n"

