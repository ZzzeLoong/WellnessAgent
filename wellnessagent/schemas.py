"""Schemas for the wellness planning business layer."""

from dataclasses import dataclass, field
from typing import Any, ClassVar

#: 识餐摘要在 trace / 时间线里的预览截断长度。
VISION_SUMMARY_PREVIEW = 120
#: 模型原始输出的存档截断长度（避免把长回答整段塞进记忆 metadata）。
VISION_RAW_MAX = 2000


@dataclass(slots=True)
class WellnessProfile:
    """Structured user profile for nutrition planning."""

    MEMORY_HEADER: ClassVar[str] = "profile_update: wellness profile"
    LIST_FIELDS: ClassVar[tuple[str, ...]] = (
        "allergies",
        "dislikes",
        "medical_notes",
        "preferred_cuisines",
        "cooking_constraints",
    )
    TEXT_FIELDS: ClassVar[tuple[str, ...]] = ("diet_pattern", "goal", "notes")
    SUPPORTED_FIELDS: ClassVar[tuple[str, ...]] = LIST_FIELDS + TEXT_FIELDS

    allergies: list[str] = field(default_factory=list)
    diet_pattern: str = ""
    goal: str = ""
    dislikes: list[str] = field(default_factory=list)
    medical_notes: list[str] = field(default_factory=list)
    preferred_cuisines: list[str] = field(default_factory=list)
    cooking_constraints: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WellnessProfile":
        """Build a normalized profile from a dictionary."""
        data = data or {}
        return cls(
            allergies=cls._normalize_list(data.get("allergies")),
            diet_pattern=cls._normalize_text(data.get("diet_pattern")),
            goal=cls._normalize_text(data.get("goal")),
            dislikes=cls._normalize_list(data.get("dislikes")),
            medical_notes=cls._normalize_list(data.get("medical_notes")),
            preferred_cuisines=cls._normalize_list(data.get("preferred_cuisines")),
            cooking_constraints=cls._normalize_list(data.get("cooking_constraints")),
            notes=cls._normalize_text(data.get("notes")),
        )

    @classmethod
    def from_memory_text(cls, text: str) -> "WellnessProfile":
        """Parse a persisted profile memory block."""
        parsed: dict[str, Any] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line == cls.MEMORY_HEADER or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key not in cls.SUPPORTED_FIELDS:
                continue
            if key in cls.LIST_FIELDS:
                parsed[key] = cls._normalize_list(value)
            else:
                parsed[key] = cls._normalize_text(value)
        return cls.from_dict(parsed)

    @staticmethod
    def _normalize_text(value: Any) -> str:
        """Normalize scalar profile values."""
        if value is None:
            return ""
        text = str(value).strip()
        if text.lower() in {"", "none", "null", "未设置", "无"}:
            return ""
        return text

    @classmethod
    def _normalize_list(cls, value: Any) -> list[str]:
        """Normalize list-like profile values from strings or iterables."""
        if value is None:
            return []
        if isinstance(value, str):
            normalized = value
            for delimiter in ("，", "、", ";", "；", "\n"):
                normalized = normalized.replace(delimiter, ",")
            parts = [item.strip() for item in normalized.split(",")]
        else:
            parts = [cls._normalize_text(item) for item in value]
        cleaned: list[str] = []
        for item in parts:
            if not item or item.lower() in {"none", "null", "未设置", "无"}:
                continue
            if item not in cleaned:
                cleaned.append(item)
        return cleaned

    def is_empty(self) -> bool:
        """Return whether the profile has any meaningful user constraint."""
        return not any(
            [
                self.allergies,
                self.diet_pattern,
                self.goal,
                self.dislikes,
                self.medical_notes,
                self.preferred_cuisines,
                self.cooking_constraints,
                self.notes,
            ]
        )

    def merged(self, updates: dict[str, Any]) -> "WellnessProfile":
        """Return a new profile with partial updates applied."""
        base = self.to_dict()
        for field_name, value in updates.items():
            if field_name not in self.SUPPORTED_FIELDS:
                continue
            if field_name in self.LIST_FIELDS:
                base[field_name] = self._normalize_list(value)
            else:
                base[field_name] = self._normalize_text(value)
        base.pop("profile_type", None)
        return self.from_dict(base)

    def without_fields(self, fields: list[str]) -> "WellnessProfile":
        """Return a new profile with specified fields cleared."""
        base = self.to_dict()
        for field_name in fields:
            if field_name not in self.SUPPORTED_FIELDS:
                continue
            base[field_name] = [] if field_name in self.LIST_FIELDS else ""
        base.pop("profile_type", None)
        return self.from_dict(base)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the profile into a stable dictionary."""
        return {
            "profile_type": "wellness_profile",
            "allergies": self.allergies,
            "diet_pattern": self.diet_pattern,
            "goal": self.goal,
            "dislikes": self.dislikes,
            "medical_notes": self.medical_notes,
            "preferred_cuisines": self.preferred_cuisines,
            "cooking_constraints": self.cooking_constraints,
            "notes": self.notes,
        }

    def to_memory_text(self) -> str:
        """Serialize the profile into retrievable text for episodic memory."""
        return "\n".join(
            [
                self.MEMORY_HEADER,
                f"allergies: {', '.join(self.allergies) if self.allergies else 'none'}",
                f"diet_pattern: {self.diet_pattern or 'none'}",
                f"goal: {self.goal or 'none'}",
                f"dislikes: {', '.join(self.dislikes) if self.dislikes else 'none'}",
                f"medical_notes: {', '.join(self.medical_notes) if self.medical_notes else 'none'}",
                f"preferred_cuisines: {', '.join(self.preferred_cuisines) if self.preferred_cuisines else 'none'}",
                f"cooking_constraints: {', '.join(self.cooking_constraints) if self.cooking_constraints else 'none'}",
                f"notes: {self.notes or 'none'}",
            ]
        )

    def to_summary_text(self) -> str:
        """Render a concise human-readable summary."""
        return "\n".join(
            [
                f"allergies: {', '.join(self.allergies) if self.allergies else 'none'}",
                f"diet_pattern: {self.diet_pattern or 'none'}",
                f"goal: {self.goal or 'none'}",
                f"dislikes: {', '.join(self.dislikes) if self.dislikes else 'none'}",
                f"medical_notes: {', '.join(self.medical_notes) if self.medical_notes else 'none'}",
                f"preferred_cuisines: {', '.join(self.preferred_cuisines) if self.preferred_cuisines else 'none'}",
                f"cooking_constraints: {', '.join(self.cooking_constraints) if self.cooking_constraints else 'none'}",
                f"notes: {self.notes or 'none'}",
            ]
        )


@dataclass
class FoodRecognition:
    """一次结构化识餐的产物（三期 R9，方案 §2.6）。

    全链路多模态解决了「图片能进模型」，但**「识餐」这个业务能力**还需要结构化
    产物——否则无法做过敏原比对、无法写感知记忆、无法进 R12 时间线。二者并存，
    不是二选一：图片**仍然**进 messages，模型在主循环里可自行再看图。

    ``risk_hits`` 由 ``safety_rules.rule_hits`` 对 ``ingredients_guess`` +
    食物名比对得出，是安全三层联动（提示层 / 确认层 / guardrails 兜底）的输入。
    """

    #: ``[{name, confidence, portion}, ...]``
    items: list[dict[str, Any]] = field(default_factory=list)
    #: 用于风险比对的食材猜测。
    ingredients_guess: list[str] = field(default_factory=list)
    #: ``(lo, hi)`` 千卡区间；模型没给则 None。
    calories_range: tuple[int, int] | None = None
    #: ``{protein_g: [lo, hi], carb_g: [...], fat_g: [...]}``
    macros: dict[str, Any] = field(default_factory=dict)
    #: 一句话摘要（供 HistoryManager 降级占位 / 时间线标题）。
    summary: str = ""
    #: 命中的用户风险词（由 safety_rules 比对得出）。
    risk_hits: list[str] = field(default_factory=list)
    #: 模型原始输出（截断存档）。
    raw: str = ""
    #: 本次识别覆盖的媒体引用。
    file_ids: list[str] = field(default_factory=list)
    #: 模型标识与耗时，便于 trace/评测回溯。
    model: str = ""
    latency_ms: int = 0

    @property
    def item_names(self) -> list[str]:
        """食物名列表（供风险比对与摘要拼装）。"""
        names: list[str] = []
        for item in self.items:
            name = str((item or {}).get("name", "")).strip()
            if name and name not in names:
                names.append(name)
        return names

    @property
    def has_risk(self) -> bool:
        return bool(self.risk_hits)

    def summary_preview(self, limit: int = VISION_SUMMARY_PREVIEW) -> str:
        """trace / 时间线用的短摘要。"""
        text = (self.summary or "").strip()
        return text[:limit]

    def to_dict(self) -> dict[str, Any]:
        """对外 JSON（``/api/chat`` 的 ``vision`` 字段 / 感知记忆 metadata）。

        ``calories_range`` 转 list：tuple 在 JSON 里本就会退化为数组，
        显式转换可让落盘 → 读回的结构保持稳定。
        """
        return {
            "items": self.items,
            "ingredients_guess": self.ingredients_guess,
            "calories_range": (
                list(self.calories_range) if self.calories_range else None
            ),
            "macros": self.macros,
            "summary": self.summary,
            "risk_hits": self.risk_hits,
            "file_ids": self.file_ids,
            "model": self.model,
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FoodRecognition":
        """从 ``to_dict`` 结构恢复（时间线 / 单测用）。"""
        data = data or {}
        calories = data.get("calories_range")
        parsed: tuple[int, int] | None = None
        if isinstance(calories, (list, tuple)) and len(calories) == 2:
            try:
                parsed = (int(calories[0]), int(calories[1]))
            except (TypeError, ValueError):
                parsed = None
        return cls(
            items=list(data.get("items") or []),
            ingredients_guess=[str(i) for i in (data.get("ingredients_guess") or [])],
            calories_range=parsed,
            macros=dict(data.get("macros") or {}),
            summary=str(data.get("summary") or ""),
            risk_hits=[str(h) for h in (data.get("risk_hits") or [])],
            raw=str(data.get("raw") or ""),
            file_ids=[str(f) for f in (data.get("file_ids") or [])],
            model=str(data.get("model") or ""),
            latency_ms=int(data.get("latency_ms") or 0),
        )
