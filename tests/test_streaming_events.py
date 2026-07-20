"""server/streaming.py 单测：二期新增事件类型的序列化（R6/R7）。"""

import json

from WellnessAgent.wellnessagent.server.streaming import StreamEvent, StreamEventType


class TestStreamEventTypes:
    def test_new_event_types_exist(self):
        assert StreamEventType.ORCHESTRATOR_TRIAGE.value == "orchestrator_triage"
        assert StreamEventType.SUBAGENT_START.value == "subagent_start"
        assert StreamEventType.SUBAGENT_RESULT.value == "subagent_result"
        assert StreamEventType.ORCHESTRATOR_AGGREGATE.value == "orchestrator_aggregate"
        assert StreamEventType.CONFIRM.value == "confirm"

    def test_confirm_sse_serialization(self):
        event = StreamEvent(
            StreamEventType.CONFIRM,
            {"confirm_id": "c-1", "kind": "safety_risk", "prompt": "确认？"},
        )
        sse = event.to_sse()
        assert sse.startswith("event: confirm\n")
        assert sse.endswith("\n\n")
        # data 行可被解析回 JSON。
        data_line = [ln for ln in sse.splitlines() if ln.startswith("data:")][0]
        payload = json.loads(data_line[len("data:"):].strip())
        assert payload["confirm_id"] == "c-1"

    def test_subagent_result_serialization(self):
        event = StreamEvent(
            StreamEventType.SUBAGENT_RESULT,
            {"subagent": "planning", "success": True, "steps": 2},
        )
        sse = event.to_sse()
        assert "event: subagent_result" in sse

