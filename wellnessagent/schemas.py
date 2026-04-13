"""Schemas for the wellness planning business layer."""

from dataclasses import dataclass, field
from typing import Any, ClassVar


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
