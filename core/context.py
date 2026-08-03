"""上下文工程（WellnessAgent 自研，R15）。

三件套：
- :class:`TokenCounter`：token 计数（tiktoken 优先，缺失降级 len//4），带缓存。
- :class:`HistoryManager`：累积 messages 的轮次压缩（整轮进出，绝不拆散
  ``assistant(tool_calls)`` 与配对 ``tool`` 消息）。
- :class:`ObservationTruncator`：过长工具输出智能截断，完整输出落盘。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

from .message import MEDIA_PLACEHOLDER, Message, make_text_part

try:  # 可选依赖，缺失时降级。
    import tiktoken

    _TIKTOKEN_AVAILABLE = True
except Exception:  # noqa: BLE001
    tiktoken = None  # type: ignore
    _TIKTOKEN_AVAILABLE = False


#: 单张图片的 token 估算成本（方案 §2.3，默认对齐 Qwen-VL 常规分辨率量级）。
DEFAULT_IMAGE_TOKEN_COST = 1024


def image_token_cost() -> int:
    """读取 ``WELLNESS_IMAGE_TOKEN_COST``（非法值回落默认）。

    R15 验收口径「token 偏差 <5%」延续到多模态：以 ``usage.prompt_tokens``
    回比校准该默认值。
    """
    try:
        value = int(os.getenv("WELLNESS_IMAGE_TOKEN_COST", str(DEFAULT_IMAGE_TOKEN_COST)))
    except (TypeError, ValueError):
        return DEFAULT_IMAGE_TOKEN_COST
    return value if value >= 0 else DEFAULT_IMAGE_TOKEN_COST


class TokenCounter:
    """带缓存的 token 计数器。"""

    def __init__(self, model: str = "gpt-4"):
        self.model = model
        self._cache: dict[str, int] = {}
        self._encoding = None
        if _TIKTOKEN_AVAILABLE:
            try:
                self._encoding = tiktoken.encoding_for_model(model)
            except Exception:
                try:
                    self._encoding = tiktoken.get_encoding("cl100k_base")
                except Exception:
                    self._encoding = None

    def _count_text(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            try:
                return len(self._encoding.encode(text))
            except Exception:
                pass
        # 降级估算。
        return max(1, len(text) // 4)

    def count_text(self, text: str) -> int:
        """无缓存计数纯文本。"""
        return self._count_text(text or "")

    def count_message(self, message: Any) -> int:
        """单条消息计数（含角色/工具字段/图片），带缓存。

        R9：图片 part 不参与文本计数，改按 ``WELLNESS_IMAGE_TOKEN_COST``
        固定成本估算；缓存 key 带上 ``image_count``，避免不同图数命中同一缓存。
        """
        role, content, extra, image_count = self._message_parts(message)
        key = f"{role}:{content}:{extra}:{image_count}"
        if key in self._cache:
            return self._cache[key]
        # 每条消息固定开销（role/分隔符）近似 4。
        total = 4 + self._count_text(content) + self._count_text(extra)
        if image_count:
            total += image_count * image_token_cost()
        self._cache[key] = total
        return total

    def count_messages(self, messages: List[Any]) -> int:
        """累计一组消息的 token。"""
        return sum(self.count_message(m) for m in messages) + 2

    @staticmethod
    def _message_parts(message: Any) -> tuple[str, str, str, int]:
        """返回 ``(role, text, extra, image_count)``（方案 §2.3）。"""
        if isinstance(message, Message):
            role = message.role
            content, image_count = TokenCounter._split_content(message.content)
            extra = ""
            if message.tool_calls:
                extra = str(message.tool_calls)
            return role, content, extra, image_count
        if isinstance(message, dict):
            role = message.get("role", "")
            content, image_count = TokenCounter._split_content(message.get("content"))
            extra = ""
            if message.get("tool_calls"):
                extra = str(message.get("tool_calls"))
            return role, content, extra, image_count
        return "", str(message), "", 0

    @staticmethod
    def _split_content(content: Any) -> tuple[str, int]:
        """把 content 拆成 ``(可计数文本, 图片数)``。

        list content 里的图片 part 只贡献 ``image_count``——绝不把
        ``wellness-file://`` 引用或（更糟的）base64 当文本计数。
        """
        if content is None:
            return "", 0
        if isinstance(content, str):
            return content, 0
        if not isinstance(content, list):
            return str(content), 0
        texts: List[str] = []
        images = 0
        for part in content:
            if not isinstance(part, dict):
                texts.append(str(part))
                continue
            ptype = part.get("type")
            if ptype == "text" or (ptype is None and "text" in part):
                texts.append(str(part.get("text", "")))
            else:
                images += 1
        return " ".join(t for t in texts if t), images

    def clear_cache(self) -> None:
        self._cache.clear()

    def get_cache_stats(self) -> dict:
        return {"cached_messages": len(self._cache), "tiktoken": _TIKTOKEN_AVAILABLE}


class HistoryManager:
    """累积 messages 的轮次压缩管理器。

    以 Message 对象存储。一"轮" = 一条 ``user`` + 其后所有 assistant/tool/summary
    消息，直到下一条 ``user``。压缩以整轮为最小单位。
    """

    def __init__(
        self,
        min_retain_rounds: Optional[int] = None,
        compression_threshold: float = 0.8,
    ):
        self.min_retain_rounds = (
            min_retain_rounds
            if min_retain_rounds is not None
            else int(os.getenv("WELLNESS_MIN_RETAIN_ROUNDS", "6"))
        )
        self.compression_threshold = compression_threshold
        self._history: List[Message] = []

    def append(self, message: Message) -> None:
        self._history.append(message)

    def extend(self, messages: List[Message]) -> None:
        self._history.extend(messages)

    def set_history(self, messages: List[Message]) -> None:
        self._history = list(messages)

    def get_history(self) -> List[Message]:
        return list(self._history)

    def clear(self) -> None:
        self._history.clear()

    def find_round_boundaries(self) -> List[int]:
        """返回每轮起始索引（``user`` 消息位置）。首个 system 不计入轮。"""
        return [i for i, m in enumerate(self._history) if m.role == "user"]

    def estimate_rounds(self) -> int:
        return len(self.find_round_boundaries())

    # -------------------------------------------------------------- R9 图片降级
    def demote_media(self, retain_rounds: Optional[int] = None) -> int:
        """把"保留窗口之外"的图片 part 换成文字占位，返回被降级的图片数。

        这是方案 §2.3 压缩决策阶梯的**中间档**：在整轮压缩之前先降媒体成本。
        占位文本优先取该图在识餐阶段产出的 ``FoodRecognition.summary``
        （由 ``Message.metadata["vision"]["<file_id>"]`` 提供），
        拿不到则退化为 ``[图片]``——这样旧图片不会白白丢失语义，
        比直接整轮压缩更保信息。

        Args:
            retain_rounds: 保留窗口轮数，缺省用 ``min_retain_rounds``。
        """
        retain = self.min_retain_rounds if retain_rounds is None else retain_rounds
        boundaries = self.find_round_boundaries()
        # 保留窗口的起点：最近 retain 轮之前的消息才允许降级。
        if retain <= 0:
            cutoff = len(self._history)
        elif len(boundaries) <= retain:
            return 0
        else:
            cutoff = boundaries[-retain]

        demoted = 0
        for index in range(min(cutoff, len(self._history))):
            message = self._history[index]
            if not message.has_media:
                continue
            new_parts, count = self._demote_parts(message)
            if not count:
                continue
            demoted += count
            # Message 构造会把"纯文本 list"拍平回 str（C2），形态自然收敛。
            replacement = Message(
                new_parts,
                message.role,
                timestamp=message.timestamp,
                metadata=message.metadata,
                tool_calls=message.tool_calls,
                tool_call_id=message.tool_call_id,
                name=message.name,
            )
            self._history[index] = replacement
        return demoted

    @staticmethod
    def _demote_parts(message: Message) -> tuple[List[dict], int]:
        """把一条消息的图片 part 替换为 ``[图片：<识餐摘要>]`` 文本 part。"""
        from .message import is_media_ref, media_ref_id

        vision_meta = (message.metadata or {}).get("vision") or {}
        parts: List[dict] = []
        demoted = 0
        for part in message.content or []:
            if not isinstance(part, dict) or part.get("type") in (None, "text"):
                parts.append(part)
                continue
            image_url = part.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            summary = ""
            if is_media_ref(url):
                entry = vision_meta.get(media_ref_id(url))
                if isinstance(entry, dict):
                    summary = str(entry.get("summary") or "")
                elif isinstance(entry, str):
                    summary = entry
            parts.append(
                make_text_part(f"[图片：{summary}]" if summary else MEDIA_PLACEHOLDER)
            )
            demoted += 1
        return parts, demoted

    def compress(self, summary: str) -> bool:
        """把早期轮压成一条 summary，保留最近 ``min_retain_rounds`` 轮。

        绝不拆散一轮内部（整轮进出）。返回是否发生压缩。
        """
        boundaries = self.find_round_boundaries()
        if len(boundaries) <= self.min_retain_rounds:
            return False

        keep_from = boundaries[-self.min_retain_rounds]

        # 保留开头的 system 消息（若存在，位于第一个 user 之前）。
        head_system = [
            m for m in self._history[:boundaries[0]] if m.role == "system"
        ] if boundaries else []

        summary_msg = Message(
            f"## 早期对话摘要\n{summary}",
            "summary",
        )
        self._history = head_system + [summary_msg] + self._history[keep_from:]
        return True


class ObservationTruncator:
    """过长工具输出截断器。"""

    def __init__(
        self,
        max_lines: int = 2000,
        max_bytes: int = 51200,
        direction: str = "head",
        output_dir: Optional[str] = None,
    ):
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self.direction = direction
        self.output_dir = Path(output_dir or os.getenv("WELLNESS_TOOL_OUTPUT_DIR", "logs/tool-output"))

    def truncate(self, tool_name: str, output: str, metadata: Optional[dict] = None) -> dict:
        """返回 ``{truncated, preview, full_output_path, stats}``。"""
        output = output or ""
        lines = output.splitlines()
        byte_len = len(output.encode("utf-8"))
        needs_truncate = len(lines) > self.max_lines or byte_len > self.max_bytes

        if not needs_truncate:
            return {
                "truncated": False,
                "preview": output,
                "full_output_path": None,
                "stats": {
                    "original_lines": len(lines),
                    "kept_lines": len(lines),
                    "original_bytes": byte_len,
                },
            }

        kept = self._truncate_lines(lines)
        preview = "\n".join(kept)
        # 再按字节兜底截断。
        if len(preview.encode("utf-8")) > self.max_bytes:
            preview = preview.encode("utf-8")[: self.max_bytes].decode("utf-8", "ignore")
        full_path = self._save_full_output(tool_name, output, metadata)
        preview = (
            f"{preview}\n\n[输出已截断，原 {len(lines)} 行 / {byte_len} 字节；"
            f"完整输出见 {full_path}]"
        )
        return {
            "truncated": True,
            "preview": preview,
            "full_output_path": full_path,
            "stats": {
                "original_lines": len(lines),
                "kept_lines": len(kept),
                "original_bytes": byte_len,
            },
        }

    def _truncate_lines(self, lines: List[str]) -> List[str]:
        if self.direction == "tail":
            return lines[-self.max_lines:]
        if self.direction == "head_tail":
            half = self.max_lines // 2
            return lines[:half] + ["... [中间省略] ..."] + lines[-half:]
        return lines[: self.max_lines]

    def _save_full_output(self, tool_name: str, output: str, metadata: Optional[dict]) -> str:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = self.output_dir / f"{tool_name}-{stamp}.txt"
            path.write_text(output, encoding="utf-8")
            return str(path)
        except Exception:
            return ""

