"""结构化识餐（三期 R9，方案 §2.6）。

全链路多模态（§2.1–2.5）解决了「图片能进模型」，但**「识餐」这个业务能力**
还需要**结构化产物**——否则：

- 无法做过敏原比对（安全三层联动没有输入）；
- 无法写感知记忆（"上周我吃过什么"检索不到）；
- 无法进 R12 时间线（没有 ``perception`` 条目）；
- ``HistoryManager.demote_media()`` 的占位文本没有语义（只能退化成 ``[图片]``）。

二者**并存**，不是二选一：识餐在 turn 开始、进主循环**之前**跑一次 VLM 调用，
之后图片**仍然进 messages**，模型在主循环里可自行再看图回答任意追问。

风险判定的关键设计：**不依赖模型自觉**。模型只负责"看到了什么"（含
``ingredients_guess`` 隐藏食材猜测），命中与否由 ``safety_rules.rule_hits``
对结构化结果做**确定性**比对得出——与 ``DietGuardrails`` 共用同一份别名表，
两道关口径不漂移（P0-3 共享模块）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

from . import safety_rules
from .prompts import (
    VISION_FOOD_RECOGNITION_PROMPT,
    vision_food_recognition_task,
)
from .schemas import VISION_RAW_MAX, FoodRecognition


#: 单次识餐最多送几张图（防止一次请求塞十几张把 token 与时延打爆）。
MAX_IMAGES_PER_CALL = 4


def _parse_json(raw: str) -> Optional[dict]:
    """从模型输出里抽出第一个 JSON 对象（与 ``orchestrator._parse_json`` 同款口径）。

    容忍 ```json 代码块包裹与前后解释性文字；抽不出来返回 None。
    """
    if not raw:
        return None
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _coerce_range(value: Any) -> Optional[tuple[int, int]]:
    """把 ``[lo, hi]`` / ``{"min":..,"max":..}`` 归一化为 ``(lo, hi)``。

    模型经常给出 ``[300, 500]``、``["300","500"]`` 甚至单个数字，全部兜住；
    实在解析不了返回 None（``FoodRecognition.calories_range`` 允许缺失）。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        lo, hi = value.get("min", value.get("lo")), value.get("max", value.get("hi"))
        value = [lo, hi]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (int(value), int(value))
    if not isinstance(value, (list, tuple)) or not value:
        return None
    nums: list[int] = []
    for item in list(value)[:2]:
        try:
            nums.append(int(float(item)))
        except (TypeError, ValueError):
            continue
    if not nums:
        return None
    if len(nums) == 1:
        return (nums[0], nums[0])
    lo, hi = nums[0], nums[1]
    return (lo, hi) if lo <= hi else (hi, lo)


def _coerce_items(value: Any) -> list[dict[str, Any]]:
    """归一化 ``items``：兼容字符串列表与 dict 列表。"""
    if not isinstance(value, (list, tuple)):
        return []
    items: list[dict[str, Any]] = []
    for raw in value:
        if isinstance(raw, str):
            name = raw.strip()
            if name:
                items.append({"name": name, "confidence": None, "portion": ""})
            continue
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("food") or "").strip()
        if not name:
            continue
        confidence: Optional[float]
        try:
            confidence = float(raw["confidence"]) if raw.get("confidence") is not None else None
        except (TypeError, ValueError):
            confidence = None
        items.append(
            {
                "name": name,
                "confidence": confidence,
                "portion": str(raw.get("portion") or "").strip(),
            }
        )
    return items


def _coerce_str_list(value: Any) -> list[str]:
    """归一化字符串列表（去空、去重、保序）。"""
    if isinstance(value, str):
        raw_items: list[Any] = [
            piece for piece in re.split(r"[,，、;；]", value) if piece.strip()
        ]
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        return []
    result: list[str] = []
    for item in raw_items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _coerce_macros(value: Any) -> dict[str, Any]:
    """归一化宏量营养素：每个键都收敛为 ``[lo, hi]``。"""
    if not isinstance(value, dict):
        return {}
    macros: dict[str, Any] = {}
    for key in ("protein_g", "carb_g", "fat_g"):
        parsed = _coerce_range(value.get(key))
        if parsed is not None:
            macros[key] = [parsed[0], parsed[1]]
    return macros


class FoodRecognizer:
    """把一组已落盘的图片 ``file_id`` 识别为 :class:`FoodRecognition`。

    依赖注入而非自己造轮子：

    - ``vision_llm``：任何有 ``invoke(messages, **kwargs) -> str`` 的对象
      （生产是 ``HelloAgentsLLM``，单测传假对象即可，不需要真模型）；
    - ``media_store``：把 ``file_id`` 解析为 data-url（**只在发送前**调用）。

    识别失败（模型不可用 / 输出不是 JSON / 图片解析不出来）一律返回
    ``None``，由调用方降级为纯多模态对话——**绝不让识餐失败拖垮整轮对话**。
    """

    def __init__(self, vision_llm: Any, media_store: Any, max_images: int = MAX_IMAGES_PER_CALL):
        self.vision_llm = vision_llm
        self.media_store = media_store
        self.max_images = max_images

    # ------------------------------------------------------------------ public
    def recognize(
        self,
        file_ids: list[str],
        profile: Any = None,
        user_id: Optional[str] = None,
        profile_summary: str = "",
        on_event: Optional[Callable[[str, dict], None]] = None,
    ) -> Optional[FoodRecognition]:
        """识别一组图片，返回结构化结果；不可用时返回 ``None``。

        Args:
            file_ids: 已落盘的媒体引用（``MediaStore.save`` 的返回值）。
            profile: 用户画像，用于 ``safety_rules.collect_risk_terms`` 风险比对。
            user_id: 媒体归属校验（避免跨用户读图）。
            profile_summary: 注入 prompt 的画像摘要文本（可选）。
            on_event: ``(event_name, payload)`` 回调，用于打 trace / 流式事件；
                抛错会被吞掉——观测不能影响主流程。
        """
        if not file_ids or self.vision_llm is None or self.media_store is None:
            return None

        selected = list(dict.fromkeys(file_ids))[: self.max_images]
        data_urls = self._resolve_data_urls(selected, user_id)
        if not data_urls:
            self._emit(on_event, "vision_error", {"message": "no resolvable media"})
            return None

        model = str(getattr(self.vision_llm, "model", "") or "")
        self._emit(
            on_event,
            "vision_start",
            {"file_ids": [fid for fid, _ in data_urls], "model": model},
        )

        started = time.monotonic()
        try:
            raw = self._invoke(data_urls, profile_summary)
        except Exception as exc:  # noqa: BLE001
            self._emit(on_event, "vision_error", {"message": str(exc)[:200]})
            return None
        latency_ms = int((time.monotonic() - started) * 1000)

        parsed = _parse_json(raw or "")
        if parsed is None:
            self._emit(
                on_event, "vision_error", {"message": "model output is not JSON"}
            )
            return None

        recognition = self._build(
            parsed,
            raw=raw or "",
            file_ids=[fid for fid, _ in data_urls],
            model=model,
            latency_ms=latency_ms,
            profile=profile,
        )
        self._emit(
            on_event,
            "vision_result",
            {
                "items_count": len(recognition.items),
                "risk_hits": recognition.risk_hits,
                "latency_ms": recognition.latency_ms,
                "summary_preview": recognition.summary_preview(),
            },
        )
        return recognition

    # ------------------------------------------------------------------ internals
    def _resolve_data_urls(
        self, file_ids: list[str], user_id: Optional[str]
    ) -> list[tuple[str, str]]:
        """把 file_id 批量解析为 data-url，丢弃解析不出来的。

        base64 只存在于返回值这个**临时列表**里，随本次调用一起消失——
        不进 messages、不进 trace、不进 SessionStore（方案 §2.2 红线）。
        """
        resolved: list[tuple[str, str]] = []
        for file_id in file_ids:
            try:
                url = self.media_store.resolve_data_url(file_id, user_id)
            except Exception:  # noqa: BLE001
                url = None
            if url:
                resolved.append((file_id, url))
        return resolved

    def _invoke(self, data_urls: list[tuple[str, str]], profile_summary: str) -> str:
        """跑一次 VLM 调用。

        用 ``invoke``（纯文本产出）而不是 ``invoke_with_tools``：识餐只要一个 JSON，
        不需要工具协议，也不该被主循环的工具白名单影响。
        """
        content: list[dict[str, Any]] = [
            {"type": "text", "text": vision_food_recognition_task(profile_summary)}
        ]
        for _file_id, url in data_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})

        messages = [
            {"role": "system", "content": VISION_FOOD_RECOGNITION_PROMPT},
            {"role": "user", "content": content},
        ]
        # 部分兼容端点不接受 response_format，故失败后退回普通调用。
        try:
            return self.vision_llm.invoke(
                messages, response_format={"type": "json_object"}, temperature=0.2
            )
        except TypeError:
            return self.vision_llm.invoke(messages)
        except Exception:  # noqa: BLE001
            return self.vision_llm.invoke(messages)

    def _build(
        self,
        parsed: dict,
        raw: str,
        file_ids: list[str],
        model: str,
        latency_ms: int,
        profile: Any,
    ) -> FoodRecognition:
        """把模型 JSON 归一化为 :class:`FoodRecognition` 并做风险比对。"""
        items = _coerce_items(parsed.get("items"))
        ingredients = _coerce_str_list(parsed.get("ingredients_guess"))
        recognition = FoodRecognition(
            items=items,
            ingredients_guess=ingredients,
            calories_range=_coerce_range(parsed.get("calories_range")),
            macros=_coerce_macros(parsed.get("macros")),
            summary=str(parsed.get("summary") or "").strip(),
            raw=(raw or "")[:VISION_RAW_MAX],
            file_ids=file_ids,
            model=model,
            latency_ms=latency_ms,
        )
        if not recognition.summary:
            # 摘要缺失时用食物名兜底：HistoryManager.demote_media() 与 R12 时间线
            # 都拿它当标题，空字符串会让旧图片白白丢失语义。
            names = recognition.item_names
            recognition.summary = "、".join(names[:3]) if names else ""
        recognition.risk_hits = self.detect_risks(recognition, profile)
        return recognition

    @staticmethod
    def detect_risks(recognition: FoodRecognition, profile: Any) -> list[str]:
        """对识别结果做**确定性**风险比对（方案 §2.6 安全联动）。

        比对文本 = 食物名 + 隐藏食材猜测 + 摘要。复用 ``safety_rules.rule_hits``，
        与 ``DietGuardrails`` 共用同一份别名表——所以「花生酱」能命中「花生」。
        """
        if profile is None:
            return []
        try:
            risk_terms = safety_rules.collect_risk_terms(profile)
        except Exception:  # noqa: BLE001
            return []
        if not risk_terms:
            return []
        haystack = " ".join(
            recognition.item_names
            + recognition.ingredients_guess
            + [recognition.summary]
        )
        try:
            return safety_rules.rule_hits(haystack, risk_terms)
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _emit(
        on_event: Optional[Callable[[str, dict], None]], event: str, payload: dict
    ) -> None:
        """观测回调（trace / 流式事件），抛错一律吞掉。"""
        if on_event is None:
            return
        try:
            on_event(event, payload)
        except Exception:  # noqa: BLE001
            pass

