"""工具权限过滤器（R6）。

职责收敛（见方案 §2.1 P0-4 说明）：``ToolFilter`` **不再包装 registry**，而是
**计算某 SubAgent 的授权白名单集合**——``filter(names)`` 产出 ``allowed`` 列表，
由 SubAgent 在每次调用时把 ``allowed`` 传给共享 ``ToolRegistry``（P0-4 无状态过滤）。

- :class:`WhitelistFilter`：仅允许显式授予的工具（SubAgent 默认策略）。
- :class:`ReadOnlyFilter`：拒绝一切写类工具（画像/记忆写入）。

``finish`` 的授权由 registry 侧 ``_normalize_allowed`` 兜底补上，这里的过滤器只表达
"业务上授予哪些工具"，不必显式含 finish。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, List, Set


class ToolFilter(ABC):
    """工具过滤器基类：计算授权白名单集合。"""

    @abstractmethod
    def is_allowed(self, tool_name: str) -> bool:
        """单个工具是否被允许。"""
        raise NotImplementedError

    def filter(self, names: Iterable[str]) -> List[str]:
        """从全部工具名中过滤出被授权的子集。"""
        return [n for n in names if self.is_allowed(n)]


class WhitelistFilter(ToolFilter):
    """白名单：仅允许显式授予的工具（SubAgent 默认策略）。"""

    def __init__(self, allowed: Iterable[str]):
        self.allowed: Set[str] = set(allowed)

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name in self.allowed


class ReadOnlyFilter(ToolFilter):
    """只读：拒绝一切写类工具（长期记忆/画像不可逆写入）。

    用于强调"绝不下放写权限"的场景；可与白名单叠加使用（先白名单再去写工具）。
    """

    # 写类工具黑名单：画像写入、记忆写入、会话短期写入等。
    DENIED: Set[str] = {
        "profile_set",
        "profile_remove",
        "memory_remember",
        "session_note",
    }

    def __init__(self, additional_denied: Iterable[str] | None = None):
        self.denied: Set[str] = set(self.DENIED)
        if additional_denied:
            self.denied.update(additional_denied)

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name not in self.denied

