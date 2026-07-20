"""SubAgent 隔离执行（R6）。

独立轻量 ReAct 子代理：**上下文隔离 + 工具子集 + 摘要返回**。

与上游 ``run_as_subagent``（清空并恢复主 agent 历史）不同，这里每次构造**独立**
``ReActAgent`` 实例，天然隔离、可并行；共享**只读** ``service``（画像/RAG/记忆读取），
不共享写路径。授权工具靠 P0-4 registry ``allowed`` 白名单在每次调用时传入（无状态、
并行安全）；子代理受限上下文经 P0-1 ``system_prompt_override`` 正规注入。

见方案 §2.2 / §2.3。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.llm import HelloAgentsLLM
from ..tools.registry import ToolRegistry
from ..tools.tool_filter import ToolFilter, WhitelistFilter
from .react_agent import ReActAgent, ReActRunResult, TERMINATED_FINISHED


@dataclass
class SubAgentResult:
    """子代理执行结果（给 Orchestrator 阅读 + trace/评测）。"""

    name: str  # profile/retrieval/planning/safety
    success: bool
    summary: str  # 给 Orchestrator 的结构化摘要（非完整历史）
    data: dict = field(default_factory=dict)  # 结构化产物（plan / hits 等）
    steps: List[dict] = field(default_factory=list)  # 该子代理 StepRecord
    metadata: dict = field(default_factory=dict)  # {steps, tools_used, duration_ms, terminated_reason}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "success": self.success,
            "summary": self.summary,
            "data": self.data,
            "steps": self.steps,
            "metadata": self.metadata,
        }


class SubAgent:
    """独立轻量 ReAct 子代理（上下文隔离 + 工具子集 + 摘要返回）。"""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        base_registry: ToolRegistry,
        tool_filter: ToolFilter,
        system_prompt: str,
        service: Any = None,
        max_steps: int = 6,
        parent_trace: Any = None,
    ):
        self.name = name
        self.llm = llm
        self.base_registry = base_registry
        self.tool_filter = tool_filter
        self.system_prompt = system_prompt
        self.service = service
        self.max_steps = max_steps
        self.parent_trace = parent_trace

    # ------------------------------------------------------------------ public
    def execute(self, task: str, context: Optional[dict] = None) -> SubAgentResult:
        """隔离执行一个子任务，返回结构化摘要。"""
        context = context or {}
        start = time.time()

        # P0-4：共享主 registry，仅传授权白名单（无需包装视图）；finish 由 registry 兜底。
        allowed = list(self.tool_filter.filter(self.base_registry.list_tools()))

        agent = ReActAgent(
            name=f"sub:{self.name}",
            llm=self.llm,
            tool_registry=self.base_registry,
            system_prompt=self.system_prompt,
            max_steps=self.max_steps,
        )
        # 子代理带 subagent 标签的子 trace（若父 trace 提供）。
        agent.trace_logger = self._subtrace(self.parent_trace)

        try:
            # P0-1：隔离运行入口，system_prompt_override 注入受限上下文 + allowed 白名单。
            result = agent.run_with_trace(
                task,
                system_prompt_override=self._compose_system(context),
                allowed_tools=allowed,
            )
        except Exception as exc:  # noqa: BLE001 - 失败隔离：不让单个子代理崩掉 pipeline
            elapsed_ms = int((time.time() - start) * 1000)
            return SubAgentResult(
                name=self.name,
                success=False,
                summary=f"[{self.name}] 子代理执行异常：{exc}",
                data={},
                steps=[],
                metadata={
                    "steps": 0,
                    "tools_used": [],
                    "duration_ms": elapsed_ms,
                    "terminated_reason": "error",
                    "error": str(exc),
                },
            )

        elapsed_ms = int((time.time() - start) * 1000)
        return self._summarize(result, agent, elapsed_ms)

    # ------------------------------------------------------------------ helpers
    def _compose_system(self, context: dict) -> str:
        """把受限上下文拼进子代理 system prompt。

        子代理独立于主对话，需要的画像/检索要点等由 Orchestrator 通过 ``context``
        显式注入（只读），避免子代理再去主动读主上下文导致污染。
        """
        sections: List[str] = [self.system_prompt]
        if context:
            sections.append("")
            sections.append("## 本次子任务上下文（只读，由编排器注入）")
            for key, value in context.items():
                rendered = value if isinstance(value, str) else _to_text(value)
                if not rendered:
                    continue
                sections.append(f"### {key}")
                sections.append(rendered)
        return "\n".join(sections)

    def _subtrace(self, parent_trace: Any):
        """返回带 subagent 标签的 trace 记录器包装；无父 trace 时返回 None。"""
        if parent_trace is None:
            return None
        return _SubAgentTraceProxy(parent_trace, self.name)

    def _summarize(
        self, result: ReActRunResult, agent: ReActAgent, elapsed_ms: int
    ) -> SubAgentResult:
        """把子代理结果压成短摘要（不把完整 messages 灌回主上下文）。"""
        tools_used: List[str] = []
        step_dicts: List[dict] = []
        for step in result.steps:
            step_dicts.append(step.to_dict())
            for tc in step.tool_calls:
                name = tc.get("name")
                if name and name != "finish":
                    tools_used.append(name)

        success = result.terminated_reason == TERMINATED_FINISHED
        raw_answer = result.final_answer or ""
        # 解析 answer 尾部可选的机器可读 JSON 块（profile/safety 子代理产出结构化 data）。
        structured = _extract_trailing_json(raw_answer)
        summary = _strip_trailing_json(raw_answer).strip()
        if len(summary) > 1200:
            summary = summary[:1200].rstrip() + "…"

        data: dict = {"final_answer": raw_answer}
        if structured:
            data.update(structured)

        return SubAgentResult(
            name=self.name,
            success=success,
            summary=summary,
            data=data,
            steps=step_dicts,
            metadata={
                "steps": len(result.steps),
                "tools_used": sorted(set(tools_used)),
                "duration_ms": elapsed_ms,
                "terminated_reason": result.terminated_reason,
            },
        )


_TRAILING_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_trailing_json(text: str) -> Optional[dict]:
    """从子代理 answer 中抽取最后一个 JSON 对象（结构化 data）。

    优先匹配 ```json ...``` 代码块；否则退化为抓取文本中最后一个平衡的 ``{...}``。
    解析失败或非 dict 返回 None（子代理未按约定产出结构化块时容错）。
    """
    if not text:
        return None
    matches = list(_TRAILING_JSON_RE.finditer(text))
    candidate: Optional[str] = None
    if matches:
        candidate = matches[-1].group(1)
    else:
        start = text.rfind("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _strip_trailing_json(text: str) -> str:
    """去掉 answer 尾部的机器可读 JSON 代码块，返回面向用户的文本部分。"""
    if not text:
        return text
    stripped = _TRAILING_JSON_RE.sub("", text)
    return stripped if stripped.strip() else text


def _to_text(value: Any) -> str:
    """把结构化上下文值渲染为可读文本（用于注入子代理 system）。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(f"- {_to_text(v)}" for v in value if v is not None)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_to_text(v)}" for k, v in value.items())
    return str(value)


class _SubAgentTraceProxy:
    """给子代理事件打 ``subagent`` 标签后转发给父 trace（方案 §2.6）。

    子代理内部的 model_output/tool_call/tool_result 等事件仍写主 trace，但带
    ``subagent`` 字段，前端/评测可按标签归组。
    """

    def __init__(self, parent_trace: Any, subagent_name: str):
        self._parent = parent_trace
        self._name = subagent_name

    def log_event(self, event: str, payload: Optional[dict] = None, step: Optional[int] = None) -> None:
        tagged = dict(payload or {})
        tagged["subagent"] = self._name
        try:
            self._parent.log_event(event, tagged, step=step)
        except Exception:
            pass

