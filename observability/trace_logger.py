"""Trace 落盘（WellnessAgent 自研，R5）。

一次会话的推理过程以双格式落盘：
- JSONL：每条事件一行，实时 flush，便于 ``jq`` 分析。
- HTML：可读回放页面，含统计汇总。

自动脱敏 API Key / Bearer / 本地用户路径。事件类型见 ``EVENT_TYPES``。
"""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


EVENT_TYPES = {
    "session_start",
    "message_written",
    "model_output",
    "tool_call",
    "tool_result",
    "circuit_open",
    "safety_block",
    "error",
    "session_end",
    # 二期 R6 编排/子代理事件（仅扩集合，向后兼容；落盘格式不变）。
    "orchestrator_triage",  # {route, reason}
    "orchestrator_dispatch",  # {subagent, task_preview}
    "orchestrator_aggregate",  # {subagents, answer_preview}
    "subagent_start",  # {subagent, tools_allowed}
    "subagent_result",  # {subagent, success, summary, steps, tools_used, duration_ms}
    # 二期 R7 HITL 确认事件（回合边界确认闭环；仅扩集合，向后兼容）。
    "confirm_request",  # {confirm_id, kind, payload}
    "confirm_resume",  # {confirm_id, decision}
}

_SANITIZE_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9\-_]{6,}"), "sk-***"),
    (re.compile(r"Bearer\s+[A-Za-z0-9\.\-_]+"), "Bearer ***"),
    (re.compile(r"/Users/[^/\s\"']+/"), "/Users/***/"),
    (re.compile(r"/home/[^/\s\"']+/"), "/home/***/"),
]


def _sanitize(value: Any) -> Any:
    """递归脱敏字符串。"""
    if isinstance(value, str):
        result = value
        for pattern, repl in _SANITIZE_PATTERNS:
            result = pattern.sub(repl, result)
        return result
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


class TraceLogger:
    """按 user_id/session_id 落盘的 trace 记录器。"""

    def __init__(
        self,
        user_id: str,
        session_id: str,
        output_dir: Optional[str] = None,
        enabled: Optional[bool] = None,
        sanitize: bool = True,
    ):
        if enabled is None:
            enabled = os.getenv("WELLNESS_TRACE_ENABLED", "true").lower() == "true"
        self.enabled = enabled
        self.sanitize = sanitize
        self.user_id = user_id
        self.session_id = session_id
        self.trace_id = session_id

        base_dir = output_dir or os.getenv("WELLNESS_TRACE_DIR", "logs/traces")
        self.dir = Path(base_dir) / user_id
        self._events: list[dict] = []
        self._started_at = datetime.now()

        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.jsonl_path = self.dir / f"trace-{session_id}.jsonl"
            self.html_path = self.dir / f"trace-{session_id}.html"
        else:
            self.jsonl_path = None
            self.html_path = None

    def log_event(
        self,
        event: str,
        payload: Optional[dict] = None,
        step: Optional[int] = None,
    ) -> None:
        """记录一条事件并实时追加到 JSONL。"""
        if not self.enabled:
            return
        payload = payload or {}
        if self.sanitize:
            payload = _sanitize(payload)
        record = {
            "ts": datetime.now().isoformat(),
            "session_id": self.session_id,
            "user_id": self.user_id,
            "step": step,
            "event": event,
            "payload": payload,
        }
        self._events.append(record)
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def finalize(self) -> dict:
        """统计并写出 HTML 回放页面，返回统计结果。"""
        stats = self._compute_stats()
        if self.enabled:
            try:
                self._write_html(stats)
            except Exception:
                pass
        return stats

    def _compute_stats(self) -> dict:
        tool_calls = sum(1 for e in self._events if e["event"] == "tool_call")
        errors = sum(1 for e in self._events if e["event"] == "error")
        safety_blocks = sum(1 for e in self._events if e["event"] == "safety_block")
        steps = sorted({e["step"] for e in self._events if e["step"] is not None})
        return {
            "trace_id": self.trace_id,
            "event_count": len(self._events),
            "tool_calls": tool_calls,
            "errors": errors,
            "safety_blocks": safety_blocks,
            "step_count": len(steps),
            "duration_ms": int((datetime.now() - self._started_at).total_seconds() * 1000),
        }

    def _write_html(self, stats: dict) -> None:
        rows = []
        for e in self._events:
            payload_text = html.escape(
                json.dumps(e["payload"], ensure_ascii=False, indent=2)
            )
            rows.append(
                f"<div class='event event-{html.escape(e['event'])}'>"
                f"<div class='meta'>#{e.get('step') if e.get('step') is not None else '-'} "
                f"<b>{html.escape(e['event'])}</b> <span>{html.escape(e['ts'])}</span></div>"
                f"<pre>{payload_text}</pre></div>"
            )
        stats_text = html.escape(json.dumps(stats, ensure_ascii=False, indent=2))
        doc = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>Trace {html.escape(self.session_id)}</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;background:#0f172a;color:#e2e8f0}}
h1{{font-size:18px}}
.stats{{background:#1e293b;padding:12px;border-radius:8px;margin-bottom:16px}}
.event{{background:#1e293b;border-left:4px solid #38bdf8;padding:8px 12px;margin:8px 0;border-radius:6px}}
.event-error{{border-left-color:#ef4444}}
.event-safety_block{{border-left-color:#f59e0b}}
.event-tool_call{{border-left-color:#a78bfa}}
.meta{{font-size:12px;color:#94a3b8;margin-bottom:4px}}
pre{{white-space:pre-wrap;word-break:break-word;margin:0;font-size:12px}}
</style></head><body>
<h1>WellnessAgent Trace · {html.escape(self.session_id)}</h1>
<div class="stats"><pre>{stats_text}</pre></div>
{''.join(rows)}
</body></html>"""
        with open(self.html_path, "w", encoding="utf-8") as fh:
            fh.write(doc)

    def __enter__(self) -> "TraceLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.finalize()

