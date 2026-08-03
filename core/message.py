"""消息系统。

累积 messages 架构需要 ``assistant`` 消息携带 ``tool_calls``、``tool`` 消息携带
``tool_call_id``。本类在保持旧位置参数构造 (``Message("x", "user")``) 兼容的前提下
扩展了这些字段，并提供 OpenAI messages 元素转换与全字段落盘序列化。

三期 R9（全链路多模态）把 ``content`` 升级为 ``str | list[ContentPart]``，使图片
真正进入 messages 数组、参与后续多轮追问。三条**硬约束**（方案 §2.1）：

- **C1**：只有 ``user`` 角色允许 list content；assistant / tool / system / summary
  恒为 ``str``（``tool`` 消息的 API 不接受多模态，且可让 ``to_openai`` 分支收敛）。
- **C2**：纯文本一律存 ``str``，**绝不**规范化成 ``[{"type":"text","text":"..."}]``。
  一期单测有 ``assert d == {"role":"user","content":"hello"}`` 这类精确断言。
- **C3**：``to_openai`` 的 ``resolver`` 缺省时，list content 降级为 ``text_content``
  （图片 part 退化为 ``[图片]`` 占位），让不关心多模态的调用方零成本。

图片在 content 里只存 ``wellness-file://<file_id>`` 短引用（~60 字节），真正的
base64 data-url 由 ``MediaStore`` 在 ``to_openai(resolver)`` 时才解析——绝不进
SessionStore / trace / token 计数（方案 §2.2）。
"""

from typing import Optional, Dict, Any, Callable, List, Literal, Union
from datetime import datetime
from pydantic import BaseModel, Field

MessageRole = Literal["user", "assistant", "system", "tool", "summary"]

# 内容分片（OpenAI / DashScope 兼容模式通用结构）。
# 文本： {"type": "text", "text": "..."}
# 图片： {"type": "image_url", "image_url": {"url": "wellness-file://<id>", "detail": "auto"}}
ContentPart = Dict[str, Any]

#: 媒体引用 scheme。content 里只存 ``wellness-file://<file_id>``。
MEDIA_URL_SCHEME = "wellness-file://"

#: resolver 缺省时图片 part 的降级占位文本（C3）。
MEDIA_PLACEHOLDER = "[图片]"

#: C1：允许携带 list content 的角色。
MULTIMODAL_ROLES = frozenset({"user"})


def is_media_ref(url: Any) -> bool:
    """判断一个 url 是否为 ``wellness-file://`` 引用。"""
    return isinstance(url, str) and url.startswith(MEDIA_URL_SCHEME)


def media_ref_id(url: str) -> str:
    """从 ``wellness-file://<id>`` 取出 file_id；非引用则原样返回。"""
    if is_media_ref(url):
        return url[len(MEDIA_URL_SCHEME):]
    return url


def make_text_part(text: str) -> ContentPart:
    """构造文本分片。"""
    return {"type": "text", "text": text}


def make_image_part(file_id: str, detail: str = "auto") -> ContentPart:
    """构造图片分片（只存 ``wellness-file://`` 引用，绝不存 base64）。"""
    return {
        "type": "image_url",
        "image_url": {"url": f"{MEDIA_URL_SCHEME}{file_id}", "detail": detail},
    }


class Message(BaseModel):
    """消息类。

    兼容旧构造方式 ``Message(content, role)``，同时支持 Function Calling 所需的
    ``tool_calls`` / ``tool_call_id`` / ``name`` 字段，以及 R9 多模态 list content。
    """

    # R9：联合类型。纯文本恒为 str（C2）；仅带媒体的 user 消息用 list（C1）。
    content: Optional[Union[str, List[ContentPart]]] = None
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

    def __init__(
        self,
        content: Optional[Union[str, List[ContentPart]]] = None,
        role: MessageRole = "user",
        **kwargs,
    ):
        """保留位置参数构造：``Message(content, role)``。

        C1/C2 归一化：
        - 非 ``user`` 角色传入 list → 拍平为纯文本 str。
        - list 里只有文本 part（无媒体） → 拍平为 str（C2：不留多余包装）。
        """
        content = self._normalize_content(content, role)
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

    # ------------------------------------------------------------------ C1/C2
    @staticmethod
    def _normalize_content(
        content: Optional[Union[str, List[ContentPart]]],
        role: str,
    ) -> Optional[Union[str, List[ContentPart]]]:
        """强制 C1（角色限制）与 C2（纯文本不变形）。"""
        if not isinstance(content, list):
            # C2：str / None 原样保留，绝不包装成 list。
            return content

        has_media = any(
            isinstance(p, dict) and p.get("type") not in (None, "text") for p in content
        )
        if role not in MULTIMODAL_ROLES or not has_media:
            # C1：非 user 角色不许 list；C2：纯文本 list 拍平回 str。
            return Message._flatten_parts(content)
        return content

    @staticmethod
    def _flatten_parts(parts: List[ContentPart]) -> str:
        """把 parts 拍平成一段文本（媒体 part 退化为占位符）。"""
        chunks: List[str] = []
        for part in parts:
            if not isinstance(part, dict):
                chunks.append(str(part))
                continue
            ptype = part.get("type")
            if ptype == "text" or (ptype is None and "text" in part):
                chunks.append(str(part.get("text", "")))
            elif ptype == "image_url":
                chunks.append(MEDIA_PLACEHOLDER)
            else:
                chunks.append(f"[{ptype or 'media'}]")
        return " ".join(c for c in chunks if c)

    # ------------------------------------------------------------------ 派生
    @property
    def text_content(self) -> str:
        """只取文本部分。

        ``str`` 直接返回；``list`` 拼接所有 text part（媒体 part 退化为 ``[图片]``）。
        所有"只要文字"的下游统一改调这里，禁止散落 ``isinstance`` 判断（方案 §10.1）。
        """
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return self._flatten_parts(self.content)

    @property
    def has_media(self) -> bool:
        """content 是 list 且含非 text part。"""
        if not isinstance(self.content, list):
            return False
        return any(
            isinstance(p, dict) and p.get("type") not in (None, "text")
            for p in self.content
        )

    def media_refs(self) -> List[str]:
        """返回所有 ``wellness-file://<id>`` 的 file_id（供 MediaStore 解析 / 时间线展示）。"""
        if not isinstance(self.content, list):
            return []
        refs: List[str] = []
        for part in self.content:
            if not isinstance(part, dict):
                continue
            url = (part.get("image_url") or {}).get("url") if isinstance(
                part.get("image_url"), dict
            ) else None
            if url is None:
                url = part.get("url")
            if is_media_ref(url):
                refs.append(media_ref_id(url))
        return refs

    # ------------------------------------------------------------------ 转换
    def to_openai(
        self, resolver: Optional[Callable[[str], str]] = None
    ) -> Dict[str, Any]:
        """转为 OpenAI chat messages 元素（含 tool_calls / tool_call_id）。

        Args:
            resolver: 把 ``file_id`` 还原为可发送 url（通常是 data-url）的回调。
                缺省时 list content 降级为 ``text_content``（C3）。
        """
        payload: Dict[str, Any] = {"role": self.role}
        # OpenAI 要求 assistant(tool_calls) 时 content 可为 None；其余角色给空串兜底。
        if self.role == "assistant" and self.tool_calls:
            payload["content"] = self.content
            payload["tool_calls"] = self.tool_calls
        elif isinstance(self.content, list):
            if resolver is None:
                # C3：不传 resolver 时退化为纯文本，调用方零成本。
                payload["content"] = self.text_content
            else:
                payload["content"] = self._resolve_parts(resolver)
        else:
            payload["content"] = self.content if self.content is not None else ""
        if self.role == "tool":
            if self.tool_call_id is not None:
                payload["tool_call_id"] = self.tool_call_id
            if self.name is not None:
                payload["name"] = self.name
        return payload

    def _resolve_parts(self, resolver: Callable[[str], str]) -> List[ContentPart]:
        """深拷贝 parts 并把 ``wellness-file://`` 换成 resolver 返回的 url。

        resolver 抛错或返回空时，该图片 part 降级为 ``[图片]`` 文本 part——
        宁可丢一张图，也不要让整轮对话因为一个坏文件直接失败。
        """
        resolved: List[ContentPart] = []
        for part in self.content or []:
            if not isinstance(part, dict):
                resolved.append(part)
                continue
            image_url = part.get("image_url")
            if not isinstance(image_url, dict) or not is_media_ref(image_url.get("url")):
                resolved.append(dict(part))
                continue
            file_id = media_ref_id(image_url["url"])
            try:
                real_url = resolver(file_id)
            except Exception:  # noqa: BLE001
                real_url = ""
            if not real_url:
                resolved.append(make_text_part(MEDIA_PLACEHOLDER))
                continue
            new_image_url = dict(image_url)
            new_image_url["url"] = real_url
            new_part = dict(part)
            new_part["image_url"] = new_image_url
            resolved.append(new_part)
        return resolved

    def to_dict(self) -> Dict[str, Any]:
        """落盘用全字段序列化（含 FC 字段与时间戳）。

        list content 本就是 JSON 可序列化的 list of dict，且只含短引用，
        故 ``SessionStore`` 无需任何改动（方案 §2.1）。
        """
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
        return f"[{self.role}] {self.text_content if isinstance(self.content, list) else self.content}"
