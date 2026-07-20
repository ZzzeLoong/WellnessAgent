"""消息系统。

累积 messages 架构需要 ``assistant`` 消息携带 ``tool_calls``、``tool`` 消息携带
``tool_call_id``。本类在保持旧位置参数构造 (``Message("x", "user")``) 兼容的前提下
扩展了这些字段，并提供 OpenAI messages 元素转换与全字段落盘序列化。
"""

from typing import Optional, Dict, Any, List, Literal
from datetime import datetime
from pydantic import BaseModel, Field

MessageRole = Literal["user", "assistant", "system", "tool", "summary"]


class Message(BaseModel):
    """消息类。

    兼容旧构造方式 ``Message(content, role)``，同时支持 Function Calling 所需的
    ``tool_calls`` / ``tool_call_id`` / ``name`` 字段。
    """

    content: Optional[str] = None
    role: MessageRole
    # D4 修复：使用 default_factory 生成时间戳，避免 ``= None`` 与校验冲突。
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
    # assistant 发起的调用（OpenAI 结构 dict 列表）。
    tool_calls: Optional[List[Dict[str, Any]]] = None
    # tool 消息对应的调用 id。
    tool_call_id: Optional[str] = None
    # tool 名（可选）。
    name: Optional[str] = None

    def __init__(self, content: Optional[str] = None, role: MessageRole = "user", **kwargs):
        """保留位置参数构造：``Message(content, role)``。"""
        data: Dict[str, Any] = {
            "content": content,
            "role": role,
            "timestamp": kwargs.get("timestamp", datetime.now()),
            "metadata": kwargs.get("metadata", {}) or {},
        }
        if "tool_calls" in kwargs:
            data["tool_calls"] = kwargs["tool_calls"]
        if "tool_call_id" in kwargs:
            data["tool_call_id"] = kwargs["tool_call_id"]
        if "name" in kwargs:
            data["name"] = kwargs["name"]
        super().__init__(**data)

    def to_openai(self) -> Dict[str, Any]:
        """转为 OpenAI chat messages 元素（含 tool_calls / tool_call_id）。"""
        payload: Dict[str, Any] = {"role": self.role}
        # OpenAI 要求 assistant(tool_calls) 时 content 可为 None；其余角色给空串兜底。
        if self.role == "assistant" and self.tool_calls:
            payload["content"] = self.content
            payload["tool_calls"] = self.tool_calls
        else:
            payload["content"] = self.content if self.content is not None else ""
        if self.role == "tool":
            if self.tool_call_id is not None:
                payload["tool_call_id"] = self.tool_call_id
            if self.name is not None:
                payload["name"] = self.name
        return payload

    def to_dict(self) -> Dict[str, Any]:
        """落盘用全字段序列化（含 FC 字段与时间戳）。"""
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat() if isinstance(self.timestamp, datetime) else self.timestamp,
            "metadata": self.metadata or {},
            "tool_calls": self.tool_calls,
            "tool_call_id": self.tool_call_id,
            "name": self.name,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        """从 ``to_dict`` 的落盘结构恢复。"""
        timestamp = data.get("timestamp")
        parsed_timestamp: Optional[datetime]
        if isinstance(timestamp, str):
            try:
                parsed_timestamp = datetime.fromisoformat(timestamp)
            except ValueError:
                parsed_timestamp = datetime.now()
        elif isinstance(timestamp, datetime):
            parsed_timestamp = timestamp
        else:
            parsed_timestamp = datetime.now()

        return cls(
            data.get("content"),
            data.get("role", "user"),
            timestamp=parsed_timestamp,
            metadata=data.get("metadata", {}) or {},
            tool_calls=data.get("tool_calls"),
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
        )

    def __str__(self) -> str:
        return f"[{self.role}] {self.content}"
