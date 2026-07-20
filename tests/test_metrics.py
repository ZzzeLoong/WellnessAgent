"""observability/metrics.py 单测（R8）：从 trace JSONL 聚合指标，口径可复现。"""

import json

from WellnessAgent.observability.metrics import aggregate_metrics


def _write_trace(base, user_id, session_id, events):
    user_dir = base / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / f"trace-{session_id}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path


def _event(event, payload=None, step=None, ts="2026-01-01T00:00:00"):
    return {"ts": ts, "session_id": "s", "user_id": "u", "step": step,
            "event": event, "payload": payload or {}}


class TestAggregateMetrics:
    def test_empty_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WELLNESS_TRACE_DIR", str(tmp_path / "none"))
        m = aggregate_metrics()
        assert m["turns"] == 0
        assert m["avg_steps_per_turn"] == 0

    def test_basic_aggregation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WELLNESS_TRACE_DIR", str(tmp_path))
        _write_trace(tmp_path, "u1", "s1", [
            _event("tool_call", {"name": "kb_search"}, step=1),
            _event("tool_call", {"name": "profile_get"}, step=1),
            _event("model_output", {"latency_ms": 100}, step=1),
            _event("model_output", {"latency_ms": 300}, step=2),
            _event("orchestrator_triage", {"route": "composite"}),
            _event("subagent_result", {"subagent": "planning", "success": True,
                                       "steps": 2, "duration_ms": 50}),
            _event("subagent_result", {"subagent": "safety", "success": False,
                                       "steps": 1, "duration_ms": 20}),
            _event("safety_block", {"action": "rewrite"}),
        ])

        m = aggregate_metrics(user_id="u1")
        assert m["turns"] == 1
        # 每回合步数取该 trace step 最大值。
        assert m["avg_steps_per_turn"] == 2
        assert m["tool_calls"] == {"kb_search": 1, "profile_get": 1}
        assert m["route_dist"] == {"composite": 1}
        assert m["safety_blocks"] == 1
        assert m["latency_ms"]["count"] == 2
        assert m["subagent_stats"]["planning"]["calls"] == 1
        assert m["subagent_stats"]["safety"]["fail_rate"] == 1.0

    def test_terminated_reason_inference(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WELLNESS_TRACE_DIR", str(tmp_path))
        # trace1：命中 confirm_request → awaiting_confirmation。
        _write_trace(tmp_path, "u1", "s1", [
            _event("confirm_request", {"confirm_id": "c-1", "kind": "safety_risk"}),
        ])
        # trace2：普通完成 → finished。
        _write_trace(tmp_path, "u1", "s2", [_event("model_output", {"latency_ms": 10})])
        m = aggregate_metrics(user_id="u1")
        assert m["turns"] == 2
        assert m["terminated_reason_dist"].get("awaiting_confirmation") == 1
        assert m["terminated_reason_dist"].get("finished") == 1
        assert m["confirm_requests"] == 1

    def test_since_filter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WELLNESS_TRACE_DIR", str(tmp_path))
        _write_trace(tmp_path, "u1", "s1", [
            _event("tool_call", {"name": "kb_search"}, step=1, ts="2026-01-01T00:00:00"),
            _event("tool_call", {"name": "kb_answer"}, step=1, ts="2026-06-01T00:00:00"),
        ])
        m = aggregate_metrics(user_id="u1", since="2026-03-01T00:00:00")
        # 只有 6 月那条被计入。
        assert m["tool_calls"] == {"kb_answer": 1}

    def test_aggregate_all_users(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WELLNESS_TRACE_DIR", str(tmp_path))
        _write_trace(tmp_path, "u1", "s1", [_event("tool_call", {"name": "a"}, step=1)])
        _write_trace(tmp_path, "u2", "s1", [_event("tool_call", {"name": "b"}, step=1)])
        m = aggregate_metrics()
        assert m["turns"] == 2
        assert m["tool_calls"] == {"a": 1, "b": 1}

