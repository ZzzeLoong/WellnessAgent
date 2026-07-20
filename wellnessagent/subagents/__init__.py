"""专职 SubAgent 工厂与注册表（R6）。

按方案 §2.3 定义四个专职 SubAgent 的**授权工具集**与 **system prompt**，并提供
``build_subagent`` 工厂 + ``make_subagent_factory``（供 Orchestrator 注入）。

| SubAgent | 授权工具（+finish 由 registry 兜底） | 职责 |
|----------|--------------------------------------|------|
| profile  | profile_get, memory_search            | 只读画像整理 + 补全建议 |
| retrieval| kb_search, kb_answer, kb_status       | 营养知识检索 |
| planning | kb_search, session_recall             | 结构化饮食规划 |
| safety   | （无外部工具，纯推理）                 | 主动安全审查 |

profile 额外叠加 :class:`ReadOnlyFilter`，从工具层杜绝写权限泄漏（即便 prompt 被绕过
也调不到写工具）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from ..prompts import (
    SUBAGENT_PLANNING_PROMPT,
    SUBAGENT_PROFILE_PROMPT,
    SUBAGENT_RETRIEVAL_PROMPT,
    SUBAGENT_SAFETY_PROMPT,
)
from ...agents.sub_agent import SubAgent
from ...tools.tool_filter import ToolFilter, WhitelistFilter, ReadOnlyFilter


# 每个 SubAgent 的授权工具集与 prompt（单一事实源）。
SUBAGENT_SPECS: Dict[str, dict] = {
    "profile": {
        "allowed": {"profile_get", "memory_search"},
        "prompt": SUBAGENT_PROFILE_PROMPT,
        "readonly": True,
    },
    "retrieval": {
        "allowed": {"kb_search", "kb_answer", "kb_status"},
        "prompt": SUBAGENT_RETRIEVAL_PROMPT,
        "readonly": False,
    },
    "planning": {
        "allowed": {"kb_search", "session_recall"},
        "prompt": SUBAGENT_PLANNING_PROMPT,
        "readonly": False,
    },
    "safety": {
        "allowed": set(),  # 纯推理，仅 finish（由 registry 兜底）。
        "prompt": SUBAGENT_SAFETY_PROMPT,
        "readonly": True,
    },
}


class _CompositeFilter(ToolFilter):
    """把多个过滤器串联为交集（AND）：白名单 + 只读黑名单叠加。"""

    def __init__(self, *filters: ToolFilter):
        self._filters = filters

    def is_allowed(self, tool_name: str) -> bool:
        return all(f.is_allowed(tool_name) for f in self._filters)


def _build_filter(spec: dict) -> ToolFilter:
    """根据 spec 构造该 SubAgent 的工具过滤器（计算授权白名单）。"""
    whitelist = WhitelistFilter(spec["allowed"])
    if spec.get("readonly"):
        return _CompositeFilter(whitelist, ReadOnlyFilter())
    return whitelist


def build_subagent(
    name: str,
    llm: Any,
    base_registry: Any,
    service: Any = None,
    max_steps: int = 6,
    parent_trace: Any = None,
) -> SubAgent:
    """按名字构造一个专职 SubAgent 实例。"""
    if name not in SUBAGENT_SPECS:
        raise ValueError(f"未知 SubAgent: {name}. 可选: {sorted(SUBAGENT_SPECS)}")
    spec = SUBAGENT_SPECS[name]
    return SubAgent(
        name=name,
        llm=llm,
        base_registry=base_registry,
        tool_filter=_build_filter(spec),
        system_prompt=spec["prompt"],
        service=service,
        max_steps=max_steps,
        parent_trace=parent_trace,
    )


def make_subagent_factory(
    llm: Any,
    base_registry: Any,
    service: Any = None,
    max_steps: int = 6,
) -> Callable[[str, Any], SubAgent]:
    """返回一个 ``factory(name, parent_trace=None) -> SubAgent``，供 Orchestrator 注入。"""

    def factory(name: str, parent_trace: Any = None) -> SubAgent:
        return build_subagent(
            name=name,
            llm=llm,
            base_registry=base_registry,
            service=service,
            max_steps=max_steps,
            parent_trace=parent_trace,
        )

    return factory


__all__ = [
    "SUBAGENT_SPECS",
    "build_subagent",
    "make_subagent_factory",
]

