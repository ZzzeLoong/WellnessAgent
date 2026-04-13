"""工具系统"""

from .base import Tool, ToolParameter
from .registry import ToolRegistry, global_registry

# 内置工具
from .builtin.search_tool import SearchTool
from .builtin.calculator import CalculatorTool
from .builtin.memory_tool import MemoryTool
from .builtin.rag_tool import RAGTool
from .builtin.note_tool import NoteTool
from .builtin.terminal_tool import TerminalTool

# 高级功能
from .chain import ToolChain, ToolChainManager, create_research_chain, create_simple_chain

__all__ = [
    # 基础工具系统
    "Tool",
    "ToolParameter",
    "ToolRegistry",
    "global_registry",

    # 内置工具
    "SearchTool",
    "CalculatorTool",
    "MemoryTool",
    "RAGTool",
    "NoteTool",
    "TerminalTool",

    # 工具链功能
    "ToolChain",
    "ToolChainManager",
    "create_research_chain",
    "create_simple_chain",
]

try:
    from .builtin.protocol_tools import MCPTool, A2ATool, ANPTool

    __all__.extend(["MCPTool", "A2ATool", "ANPTool"])
except Exception:
    pass

try:
    from .builtin.bfcl_evaluation_tool import BFCLEvaluationTool

    __all__.append("BFCLEvaluationTool")
except Exception:
    pass

try:
    from .builtin.gaia_evaluation_tool import GAIAEvaluationTool

    __all__.append("GAIAEvaluationTool")
except Exception:
    pass

try:
    from .builtin.llm_judge_tool import LLMJudgeTool

    __all__.append("LLMJudgeTool")
except Exception:
    pass

try:
    from .builtin.win_rate_tool import WinRateTool

    __all__.append("WinRateTool")
except Exception:
    pass

try:
    from .builtin.rl_training_tool import RLTrainingTool

    __all__.append("RLTrainingTool")
except Exception:
    pass

try:
    from .async_executor import (
        AsyncToolExecutor,
        run_parallel_tools,
        run_batch_tool,
        run_parallel_tools_sync,
        run_batch_tool_sync,
    )

    __all__.extend(
        [
            "AsyncToolExecutor",
            "run_parallel_tools",
            "run_batch_tool",
            "run_parallel_tools_sync",
            "run_batch_tool_sync",
        ]
    )
except Exception:
    pass
