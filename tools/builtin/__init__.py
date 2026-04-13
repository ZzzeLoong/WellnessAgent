"""内置工具模块。

仅保证核心工具可直接导入。评测、协议、训练类工具按可用依赖进行可选导入，
避免业务场景仅使用基础能力时被额外模块阻塞。
"""

from .search_tool import SearchTool
from .calculator import CalculatorTool
from .memory_tool import MemoryTool
from .rag_tool import RAGTool
from .note_tool import NoteTool
from .terminal_tool import TerminalTool

__all__ = [
    "SearchTool",
    "CalculatorTool",
    "MemoryTool",
    "RAGTool",
    "NoteTool",
    "TerminalTool",
]

try:
    from .protocol_tools import MCPTool, A2ATool, ANPTool

    __all__.extend(["MCPTool", "A2ATool", "ANPTool"])
except Exception:
    pass

try:
    from .bfcl_evaluation_tool import BFCLEvaluationTool

    __all__.append("BFCLEvaluationTool")
except Exception:
    pass

try:
    from .gaia_evaluation_tool import GAIAEvaluationTool

    __all__.append("GAIAEvaluationTool")
except Exception:
    pass

try:
    from .llm_judge_tool import LLMJudgeTool

    __all__.append("LLMJudgeTool")
except Exception:
    pass

try:
    from .win_rate_tool import WinRateTool

    __all__.append("WinRateTool")
except Exception:
    pass