"""指标聚合（WellnessAgent 自研，R8）。

从一期落盘的 trace JSONL（``logs/traces/<user>/trace-*.jsonl``）**只读聚合**，不引入
新存储。指标口径**只依赖已落盘事件字段**（一期已写 ``usage`` / ``latency_ms`` /
``status``；二期扩了 orchestrator/subagent/confirm 事件），可复现。

对外提供 :func:`aggregate_metrics`，支持按 ``user_id`` 与时间窗（``since`` ISO 字符串）
过滤。见方案 §4.1。
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional


def _trace_dir() -> Path:
    return Path(os.getenv("WELLNESS_TRACE_DIR", "logs/traces"))


def _iter_trace_files(user_id: Optional[str]) -> Iterable[Path]:
    """遍历 trace JSONL 文件。user_id 为空则遍历所有用户。"""
    base = _trace_dir()
    if not base.exists():
        return []
    files: list[Path] = []
    user_dirs = [base / user_id] if user_id else [d for d in base.iterdir() if d.is_dir()]
    for user_dir in user_dirs:
        if not user_dir.exists():
            continue
        files.extend(sorted(user_dir.glob("trace-*.jsonl")))
    return files


def _iter_events(user_id: Optional[str], since: Optional[str]):
    """逐条产出满足过滤条件的事件记录（含所属 trace 的分组信息）。

    以 ``(trace_id, event_record)`` 形式产出，便于按 trace 聚合"每回合步数"等。
    """
    for path in _iter_trace_files(user_id):
        trace_id = path.stem[len("trace-"):]
        events: list[dict] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if since and str(rec.get("ts", "")) < since:
                continue
            events.append(rec)
        if events:
            yield trace_id, events


def _percentile(values: list[float], pct: float) -> Optional[float]:
    """线性插值百分位；空列表返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = pct / 100 * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return round(ordered[low] + (ordered[high] - ordered[low]) * frac, 2)


def aggregate_metrics(
    user_id: Optional[str] = None, since: Optional[str] = None
) -> dict[str, Any]:
    """遍历 trace JSONL，产出汇总指标（方案 §4.1）。

    Args:
        user_id: 仅聚合某用户；None 聚合所有用户。
        since: ISO 时间字符串，仅统计该时间之后的事件；None 不过滤。

    Returns:
        含 turns / avg_steps_per_turn / tool_calls / terminated_reason_dist /
        safety_blocks / circuit_open / confirm_requests / route_dist /
        subagent_stats / latency_ms 的字典。
    """
    turns = 0
    total_steps = 0
    tool_calls: Counter = Counter()
    terminated_reason_dist: Counter = Counter()
    route_dist: Counter = Counter()
    safety_blocks = 0
    circuit_open = 0
    confirm_requests = 0
    confirm_resumes = 0
    latencies: list[float] = []

    # SubAgent 统计：按名字聚合 calls / steps / duration / fail。
    sub_calls: Counter = Counter()
    sub_steps: defaultdict = defaultdict(int)
    sub_duration: defaultdict = defaultdict(float)
    sub_fail: Counter = Counter()

    for _trace_id, events in _iter_events(user_id, since):
        turns += 1
        # 每回合步数：取该 trace 里 step 字段最大值（step 从 1 累加）。
        step_values = [e["step"] for e in events if e.get("step") is not None]
        if step_values:
            total_steps += max(step_values)

        # 终止原因：优先取显式 session_end 事件；否则从事件类型推断（零侵入）。
        trace_events = {e.get("event") for e in events}
        explicit_reason = next(
            (
                (e.get("payload") or {}).get("terminated_reason")
                for e in events
                if e.get("event") == "session_end"
                and (e.get("payload") or {}).get("terminated_reason")
            ),
            None,
        )
        if explicit_reason:
            terminated_reason_dist[explicit_reason] += 1
        elif "confirm_request" in trace_events:
            terminated_reason_dist["awaiting_confirmation"] += 1
        elif "error" in trace_events:
            terminated_reason_dist["error"] += 1
        else:
            terminated_reason_dist["finished"] += 1

        for e in events:
            event = e.get("event")
            payload = e.get("payload") or {}
            if event == "tool_call":
                name = payload.get("name")
                if name:
                    tool_calls[name] += 1
            elif event == "safety_block":
                safety_blocks += 1
            elif event == "circuit_open":
                circuit_open += 1
            elif event == "confirm_request":
                confirm_requests += 1
            elif event == "confirm_resume":
                confirm_resumes += 1
            elif event == "orchestrator_triage":
                route = payload.get("route")
                if route:
                    route_dist[route] += 1
            elif event == "model_output":
                latency = payload.get("latency_ms")
                if isinstance(latency, (int, float)):
                    latencies.append(float(latency))
            elif event == "subagent_result":
                name = payload.get("subagent")
                if name:
                    sub_calls[name] += 1
                    sub_steps[name] += int(payload.get("steps") or 0)
                    sub_duration[name] += float(payload.get("duration_ms") or 0)
                    if not payload.get("success", True):
                        sub_fail[name] += 1

    subagent_stats: dict[str, dict] = {}
    for name, calls in sub_calls.items():
        subagent_stats[name] = {
            "calls": calls,
            "avg_steps": round(sub_steps[name] / calls, 2) if calls else 0,
            "avg_duration_ms": round(sub_duration[name] / calls, 2) if calls else 0,
            "fail_rate": round(sub_fail[name] / calls, 3) if calls else 0,
        }

    return {
        "turns": turns,
        "avg_steps_per_turn": round(total_steps / turns, 2) if turns else 0,
        "tool_calls": dict(tool_calls),
        "terminated_reason_dist": dict(terminated_reason_dist),
        "safety_blocks": safety_blocks,
        "circuit_open": circuit_open,
        "confirm_requests": confirm_requests,
        "confirm_resumes": confirm_resumes,
        "route_dist": dict(route_dist),
        "subagent_stats": subagent_stats,
        "latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "count": len(latencies),
        },
    }

