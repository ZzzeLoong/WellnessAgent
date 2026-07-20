"""会话消息落盘（WellnessAgent 自研，R17）。

按 ``user_id`` / ``session_id`` 组织，把当前会话的原始对话消息（system/user/
assistant/tool 全量，含 ``tool_calls`` / ``tool_call_id``）落盘为 JSON，重启可续。

与 episodic 长期记忆、working memory 各自独立，互不覆盖。
"""

from __future__ import annotations

import json
import os
import random
import string
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from .message import Message


class SessionStore:
    """Per-user conversation persistence."""

    def __init__(self, session_dir: Optional[str] = None):
        base = session_dir or os.getenv("WELLNESS_SESSION_DIR", "logs/sessions")
        self.session_dir = Path(base)

    # ------------------------------------------------------------------ paths
    def _user_dir(self, user_id: str) -> Path:
        return self.session_dir / user_id

    def _session_path(self, user_id: str, session_id: str) -> Path:
        return self._user_dir(user_id) / f"{session_id}.json"

    # ------------------------------------------------------------------ ids
    @staticmethod
    def new_session_id() -> str:
        """生成 ``s-YYYYMMDD-HHMMSS-xxxx`` 形式的会话 id。"""
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        return f"s-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{suffix}"

    # ------------------------------------------------------------------ io
    def save(
        self,
        user_id: str,
        session_id: str,
        messages: List[Message],
        metadata: Optional[dict] = None,
    ) -> str:
        """原子落盘会话消息，返回文件路径。"""
        user_dir = self._user_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = self._session_path(user_id, session_id)

        now = datetime.now().isoformat()
        created_at = now
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                created_at = existing.get("created_at", now)
            except Exception:
                pass

        payload = {
            "session_id": session_id,
            "user_id": user_id,
            "created_at": created_at,
            "updated_at": now,
            "messages": [self._message_to_dict(m) for m in messages],
            "metadata": metadata or {},
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return str(path)

    def load(self, user_id: str, session_id: str) -> Optional[dict]:
        """加载会话，返回 ``{messages: list[Message], metadata, ...}``；不存在返回 None。"""
        path = self._session_path(user_id, session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        messages = [Message.from_dict(m) for m in data.get("messages", [])]
        return {
            "session_id": data.get("session_id", session_id),
            "user_id": data.get("user_id", user_id),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "messages": messages,
            "metadata": data.get("metadata", {}),
        }

    def latest_session_id(self, user_id: str) -> Optional[str]:
        """返回最近活跃的会话 id（按 updated_at 倒序）。"""
        sessions = self.list_sessions(user_id)
        return sessions[0]["session_id"] if sessions else None

    def list_sessions(self, user_id: str) -> List[dict]:
        """列出 user 的所有会话（session_id + 时间 + 轮数），按 updated_at 倒序。"""
        user_dir = self._user_dir(user_id)
        if not user_dir.exists():
            return []
        sessions: List[dict] = []
        for path in user_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            messages = data.get("messages", [])
            rounds = sum(1 for m in messages if m.get("role") == "user")
            sessions.append(
                {
                    "session_id": data.get("session_id", path.stem),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "message_count": len(messages),
                    "round_count": rounds,
                }
            )
        sessions.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
        return sessions

    def delete(self, user_id: str, session_id: str) -> bool:
        """删除一个会话文件。"""
        path = self._session_path(user_id, session_id)
        if path.exists():
            path.unlink()
            return True
        return False

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _message_to_dict(message: Any) -> dict:
        """兼容 Message 对象与已是 dict 的消息。"""
        if isinstance(message, Message):
            return message.to_dict()
        if isinstance(message, dict):
            return message
        # 兜底：尽力序列化。
        return {"role": getattr(message, "role", "user"), "content": str(message)}

