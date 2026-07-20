"""pytest 共享配置。

把仓库父目录加入 sys.path，使 ``WellnessAgent`` 可作为顶层包被导入
（``agents/react_agent.py`` 内部使用 ``from ..core`` 相对导入，需要其父包存在）。
同时保留 ``from core.xxx`` 直接导入风格：WellnessAgent 目录本身也在 path 中。
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]      # .../WellnessAgent
_REPO_PARENT = _REPO_ROOT.parent                       # .../tzl

for p in (str(_REPO_PARENT), str(_REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

