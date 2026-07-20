"""ReAct Agent（重写内核）。

累积 ``messages`` 数组 + 原生 Function Calling 主循环；不支持 FC 的 provider 走
JSON 回退（同样基于累积 messages，仅 tool_calls 来源不同）。结束判定用内置
``finish`` 工具。可选接入上下文压缩、工具输出截断、trace、Guardrails。

详见 docs/tech-design-phase1.md 第 2、4、5、7 节。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, List, Optional

from ..core.agent import Agent
from ..core.llm import HelloAgentsLLM
from ..core.config import Config
from ..core.context import HistoryManager, ObservationTruncator, TokenCounter
from ..core.llm_response import LLMToolResponse, ToolCall
from ..core.message import Message
from ..tools.registry import FINISH_TOOL_NAME, ToolRegistry
from ..tools.response import ToolResponse, ToolStatus


# 对外稳定的终止原因集合。
TERMINATED_FINISHED = "finished"
TERMINATED_INVALID_ACTION = "invalid_action"
TERMINATED_MAX_STEPS = "max_steps"
TERMINATED_LLM_EMPTY = "llm_empty_response"


@dataclass
class StepRecord:
    """单步 ReAct 执行记录（从累积 messages 派生）。

    新结构以 ``tool_calls`` / ``tool_results`` 为主（无旧别名）：一步
    （``assistant``）可同时发起多个工具调用，``tool_results`` 与之一一对应。
    """

    index: int
    role: str  # "assistant" | "tool"
    thought: Optional[str]
    tool_calls: List[dict] = field(default_factory=list)
    tool_results: List[dict] = field(default_factory=list)
    source: str = "function_calling"
    raw_response: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReActRunResult:
    """结构化 ReAct 执行结果。"""

    final_answer: str
    steps: List[StepRecord]
    terminated_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_answer": self.final_answer,
            "steps": [step.to_dict() for step in self.steps],
            "terminated_reason": self.terminated_reason,
        }


class ReActAgent(Agent):
    """累积 messages + Function Calling 的 ReAct Agent。"""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        tool_registry: Optional[ToolRegistry] = None,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        max_steps: int = 8,
        fallback_prompt_suffix: str = "",
        context_window: Optional[int] = None,
    ):
        super().__init__(name, llm, system_prompt, config)
        self.tool_registry = tool_registry or ToolRegistry()
        self.max_steps = max_steps
        self.fallback_prompt_suffix = fallback_prompt_suffix

        self.messages: List[Message] = []

        self.context_window = context_window or int(
            os.getenv("WELLNESS_CONTEXT_WINDOW", "262144")
        )
        self.token_counter = TokenCounter(model="gpt-4")
        self.history_manager = HistoryManager()
        self.truncator = ObservationTruncator()

        # 可选钩子（由业务 agent 注入）。
        self.build_system_prompt: Optional[Callable[[str], str]] = None
        self.trace_logger = None
        self.guardrails = None
        self.get_guardrail_profile: Optional[Callable[[], Any]] = None
        self.smart_compression = (
            os.getenv("WELLNESS_SMART_COMPRESSION", "false").lower() == "true"
        )
        self.get_summary_llm: Optional[Callable[[], Any]] = None

    def add_tool(self, tool):
        """兼容旧接口：注册 Tool 对象。"""
        self.tool_registry.register_tool(tool)

    # ------------------------------------------------------------------ public
    def run(self, input_text: str, **kwargs) -> str:
        return self.run_with_trace(input_text, **kwargs).final_answer

    def run_with_trace(
        self,
        input_text: str,
        system_prompt_override: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        **kwargs,
    ) -> ReActRunResult:
        """非流式：驱动内部事件生成器，只取最终结果。

        Args:
            input_text: 用户输入。
            system_prompt_override: P0-1 隔离运行入口。传入时**一次性**用它作为本次
                运行的 system 消息，绕过 ``build_system_prompt`` 钩子（子代理注入受限
                上下文的正规入口，替代猴补丁）。不传=一期行为。
            allowed_tools: P0-1/P0-4 授权工具白名单。传入时本次运行只暴露/执行白名单内
                的工具（``finish`` 恒被允许）。不传=一期行为（全部工具）。
        """
        result: Optional[ReActRunResult] = None
        for event in self._iter_events(
            input_text,
            system_prompt_override=system_prompt_override,
            allowed_tools=allowed_tools,
        ):
            if event["type"] == "result":
                result = event["result"]
        assert result is not None  # 生成器保证最终必产出 result
        return result

    def stream_run(
        self,
        input_text: str,
        system_prompt_override: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        **kwargs,
    ):
        """流式：逐事件产出（step_start / tool_call / tool_result / step / result）。

        供业务 agent 的 ``chat_stream`` 消费；事件为普通 dict，业务层自行映射为
        SSE。最终答案分段由业务层对 ``result.final_answer`` 切片（方案 §6.2 一期做法）。

        ``system_prompt_override`` / ``allowed_tools`` 语义同 ``run_with_trace``（P0-1）。
        """
        yield from self._iter_events(
            input_text,
            system_prompt_override=system_prompt_override,
            allowed_tools=allowed_tools,
        )

    # ------------------------------------------------------------------ core loop
    def _iter_events(
        self,
        input_text: str,
        system_prompt_override: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
    ):
        """累积 messages + FC 主循环，逐步 yield 事件，最后 yield 一个 result 事件。"""
        use_fc = self.llm.supports_function_calling()
        source = "function_calling" if use_fc else "json_fallback"

        self._refresh_system_message(input_text, system_prompt_override)
        self._append_message(Message(input_text, "user"))
        if self.trace_logger is not None:
            self.trace_logger.log_event(
                "message_written", {"role": "user", "content": input_text}
            )

        tool_schemas = self.tool_registry.build_tool_schemas(
            include_finish=True, allowed=allowed_tools
        )
        steps: List[StepRecord] = []
        step_index = 0

        while step_index < self.max_steps:
            step_index += 1
            self._maybe_compress()
            yield {"type": "step_start", "step": step_index}

            try:
                resp = self._invoke_fc(tool_schemas) if use_fc else self._invoke_fallback()
            except Exception as exc:  # noqa: BLE001
                if self.trace_logger is not None:
                    self.trace_logger.log_event(
                        "error", {"message": str(exc)}, step=step_index
                    )
                yield {"type": "error", "step": step_index, "message": str(exc)}
                yield self._finalize_event(
                    input_text, "抱歉，模型调用出现异常，请稍后重试。", steps,
                    TERMINATED_LLM_EMPTY,
                )
                return

            if self.trace_logger is not None:
                self.trace_logger.log_event(
                    "model_output",
                    {
                        "content": resp.content,
                        "tool_calls": [
                            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                            for tc in resp.tool_calls
                        ],
                        "usage": resp.usage,
                        "latency_ms": resp.latency_ms,
                    },
                    step=step_index,
                )
            if resp.content:
                yield {"type": "thinking", "step": step_index, "content": resp.content}

            # 无 tool_calls：兜底把 content 当最终答案（implicit finish）。
            if not resp.tool_calls:
                if resp.content is None or not str(resp.content).strip():
                    if not steps:
                        yield self._finalize_event(
                            input_text, "抱歉，模型未能返回有效响应。", steps,
                            TERMINATED_LLM_EMPTY,
                        )
                        return
                    yield self._finalize_event(input_text, "", steps, TERMINATED_FINISHED)
                    return
                self._append_message(Message(resp.content, "assistant"))
                step = StepRecord(
                    index=step_index,
                    role="assistant",
                    thought=resp.content,
                    source=source,
                    raw_response=resp.content,
                )
                steps.append(step)
                yield {"type": "step", "step": step_index, "record": step}
                yield self._finalize_event(
                    input_text, resp.content, steps, TERMINATED_FINISHED
                )
                return

            # finish 调用。
            finish_answer = self._extract_finish_answer(resp.tool_calls)
            if finish_answer is not None:
                self._append_message(
                    Message(
                        resp.content,
                        "assistant",
                        tool_calls=self._tool_calls_to_openai(resp.tool_calls),
                    )
                )
                step = StepRecord(
                    index=step_index,
                    role="assistant",
                    thought=resp.content,
                    tool_calls=[
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in resp.tool_calls
                    ],
                    source=source,
                )
                steps.append(step)
                yield {"type": "step", "step": step_index, "record": step}
                yield self._finalize_event(
                    input_text, finish_answer, steps, TERMINATED_FINISHED
                )
                return

            # 普通工具调用。
            self._append_message(
                Message(
                    resp.content,
                    "assistant",
                    tool_calls=self._tool_calls_to_openai(resp.tool_calls),
                )
            )

            step_tool_results: List[dict] = []
            for tc in resp.tool_calls:
                tool_input = self._parse_arguments(tc)
                if self.trace_logger is not None:
                    self.trace_logger.log_event(
                        "tool_call",
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments},
                        step=step_index,
                    )
                yield {
                    "type": "tool_call",
                    "step": step_index,
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                }
                tool_response = self.tool_registry.execute_tool(
                    tc.name, tool_input, allowed=allowed_tools
                )
                truncated = self.truncator.truncate(tc.name, tool_response.text)
                content_text = truncated["preview"]

                if self.trace_logger is not None:
                    if tool_response.error_info and (
                        tool_response.error_info.get("code") == "CIRCUIT_OPEN"
                    ):
                        self.trace_logger.log_event(
                            "circuit_open", {"tool": tc.name}, step=step_index
                        )
                    self.trace_logger.log_event(
                        "tool_result",
                        {
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "status": tool_response.status.value,
                            "content": content_text,
                            "truncated": truncated["truncated"],
                        },
                        step=step_index,
                    )

                self._append_message(
                    Message(content_text, "tool", tool_call_id=tc.id, name=tc.name)
                )
                result_entry = {
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "status": tool_response.status.value,
                    "content": content_text,
                }
                step_tool_results.append(result_entry)
                yield {"type": "tool_result", "step": step_index, **result_entry}

            step = StepRecord(
                index=step_index,
                role="assistant",
                thought=resp.content,
                tool_calls=[
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in resp.tool_calls
                ],
                tool_results=step_tool_results,
                source=source,
                raw_response=resp.content,
            )
            steps.append(step)
            yield {"type": "step", "step": step_index, "record": step}

        yield self._finalize_event(
            input_text,
            "抱歉，我无法在限定步数内完成这个任务。",
            steps,
            TERMINATED_MAX_STEPS,
        )

    # ------------------------------------------------------------------ invoke
    def _invoke_fc(self, tool_schemas: List[dict]) -> LLMToolResponse:
        """FC 路径：原生返回 tool_calls。"""
        openai_messages = [m.to_openai() for m in self.messages]
        return self.llm.invoke_with_tools(openai_messages, tool_schemas, tool_choice="auto")

    def _invoke_fallback(self) -> LLMToolResponse:
        """回退路径：invoke 文本 + JSON 解析 → 构造成 ToolCall。"""
        openai_messages = [m.to_openai() for m in self.messages]
        llm_kwargs: dict[str, Any] = {"temperature": 0.1}
        try:
            text = self.llm.invoke(openai_messages, response_format={"type": "json_object"}, **llm_kwargs)
        except Exception:
            text = self.llm.invoke(openai_messages, **llm_kwargs)

        parsed = self._parse_json_output(text or "")
        if parsed is None:
            # 无法解析成动作：把文本当作最终答案兜底。
            return LLMToolResponse(content=text, tool_calls=[], model=self.llm.model)

        thought, name, arguments = parsed
        tc = ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=json.dumps(arguments, ensure_ascii=False))
        return LLMToolResponse(content=thought, tool_calls=[tc], model=self.llm.model)

    # ------------------------------------------------------------------ finish
    def _extract_finish_answer(self, tool_calls: List[ToolCall]) -> Optional[str]:
        """若存在 finish 调用，返回其 answer；否则 None。"""
        for tc in tool_calls:
            if tc.name == FINISH_TOOL_NAME:
                try:
                    args = json.loads(tc.arguments or "{}")
                except Exception:
                    args = {}
                answer = args.get("answer", "")
                return str(answer) if answer is not None else ""
        return None

    # ------------------------------------------------------------------ helpers
    def _append_message(self, message: Message) -> None:
        self.messages.append(message)
        self.history_manager.append(message)

    def _refresh_system_message(
        self, input_text: str, system_prompt_override: Optional[str] = None
    ) -> None:
        """每轮开始刷新首条 system 消息（角色 + 安全 + 画像 + 记忆 + working 摘要）。

        P0-1：传入 ``system_prompt_override`` 时**直接用它**作为 system 内容，绕过
        ``build_system_prompt`` 钩子（子代理隔离运行的正规入口）。
        """
        if system_prompt_override is not None:
            content = system_prompt_override
        else:
            content = self.system_prompt or ""
            if self.build_system_prompt is not None:
                try:
                    content = self.build_system_prompt(input_text)
                except Exception:
                    content = self.system_prompt or ""
        if not self.llm.supports_function_calling() and self.fallback_prompt_suffix:
            tools_desc = self.tool_registry.get_tools_description()
            content = content + self.fallback_prompt_suffix.format(tools=tools_desc)

        system_msg = Message(content, "system")
        if self.messages and self.messages[0].role == "system":
            self.messages[0] = system_msg
        else:
            self.messages.insert(0, system_msg)
        self.history_manager.set_history(self.messages)

    def _maybe_compress(self) -> None:
        """token 超阈值时按整轮压缩历史。"""
        try:
            total = self.token_counter.count_messages(self.messages)
        except Exception:
            return
        threshold = self.context_window * self.history_manager.compression_threshold
        if total < threshold:
            return
        summary = self._build_compression_summary()
        if self.history_manager.compress(summary):
            self.messages = self.history_manager.get_history()
            if self.trace_logger is not None:
                self.trace_logger.log_event(
                    "message_written",
                    {"role": "summary", "compressed": True, "tokens_before": total},
                )

    def _build_compression_summary(self) -> str:
        """构造压缩摘要：智能摘要（LLM）优先，否则统计摘要。"""
        if self.smart_compression and self.get_summary_llm is not None:
            llm = None
            try:
                llm = self.get_summary_llm()
            except Exception:
                llm = None
            if llm is not None:
                try:
                    transcript = "\n".join(
                        f"[{m.role}] {(m.content or '')[:400]}" for m in self.messages
                    )
                    prompt = [
                        {"role": "system", "content": "请把以下多轮对话压缩为要点摘要，保留用户长期约束、关键决策与未完成事项，200 字内。"},
                        {"role": "user", "content": transcript},
                    ]
                    return llm.invoke(prompt)
                except Exception:
                    pass
        # 统计摘要。
        rounds = self.history_manager.estimate_rounds()
        tool_names: List[str] = []
        for m in self.messages:
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    fn = (tc.get("function") or {}).get("name")
                    if fn:
                        tool_names.append(fn)
        unique_tools = sorted(set(tool_names))
        return (
            f"共约 {rounds} 轮对话，调用工具 {len(tool_names)} 次"
            f"（涉及：{', '.join(unique_tools) or '无'}）。此前细节已归档。"
        )

    @staticmethod
    def _tool_calls_to_openai(tool_calls: List[ToolCall]) -> List[dict]:
        """把 ToolCall 列表转为 OpenAI assistant.tool_calls 结构。"""
        return [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments or "{}"},
            }
            for tc in tool_calls
        ]

    @staticmethod
    def _parse_arguments(tc: ToolCall) -> Any:
        """解析工具入参：单参数 input 退化为字符串，其余保留 dict。"""
        try:
            args = json.loads(tc.arguments or "{}")
        except Exception:
            return tc.arguments or ""
        if not isinstance(args, dict):
            return args
        if set(args.keys()) == {"input"}:
            return args.get("input", "")
        if not args:
            return ""
        return args

    # ------------------------------------------------------------------ finalize
    def _finalize_event(
        self,
        input_text: str,
        final_answer: str,
        steps: List[StepRecord],
        terminated_reason: str,
    ) -> dict:
        """把 ``_finalize`` 的结果包装成生成器的 ``result`` 事件。"""
        result = self._finalize(input_text, final_answer, steps, terminated_reason)
        return {"type": "result", "result": result}

    def _finalize(
        self,
        input_text: str,
        final_answer: str,
        steps: List[StepRecord],
        terminated_reason: str,
    ) -> ReActRunResult:
        """终止前 Guardrails 复核，写历史并返回结果。"""
        safe_answer = final_answer
        if self.guardrails is not None and terminated_reason == TERMINATED_FINISHED:
            profile = None
            if self.get_guardrail_profile is not None:
                try:
                    profile = self.get_guardrail_profile()
                except Exception:
                    profile = None
            try:
                result = self.guardrails.check(final_answer, profile)
                if result.action in {"rewrite", "block"}:
                    safe_answer = result.safe_text
                    if self.trace_logger is not None:
                        self.trace_logger.log_event(
                            "safety_block",
                            {
                                "action": result.action,
                                "hits": result.hits,
                                "reason": result.reason,
                            },
                        )
            except Exception:
                safe_answer = final_answer

        # P0-2：对话历史收敛为 ``self.messages`` 单一源，不再冗余写基类 ``_history``。
        # ``_get_completed_turns`` / ``_recent_dialogue_window`` 改为从 ``messages`` 派生。
        # 注意：finish 场景下末条 assistant 已在主循环写入 messages（可能是 tool_calls-only），
        # 这里把经 guardrails 复核后的 ``safe_answer`` 作为该回合面向用户的最终文本补记，
        # 使 messages 中"最后一条 user 之后存在可读 assistant 文本"，供轮次派生与后续续接。
        if safe_answer:
            self._append_message(Message(safe_answer, "assistant"))
        return ReActRunResult(
            final_answer=safe_answer,
            steps=steps,
            terminated_reason=terminated_reason,
        )

    # ------------------------------------------------------------------ json fallback parse
    def _parse_json_output(self, text: str):
        """解析回退路径 JSON → (thought, name, arguments)；无效返回 None。"""
        payload = self._extract_json_object(text)
        if payload is None:
            return None
        try:
            data = json.loads(payload)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        thought = str(data.get("thought", "")).strip() or None
        action = data.get("action")
        if not isinstance(action, dict):
            return None
        action_type = str(action.get("type", "")).strip().lower()
        if action_type == "finish":
            answer = action.get("answer", "")
            return thought, FINISH_TOOL_NAME, {"answer": "" if answer is None else str(answer)}
        if action_type == "tool":
            name = str(action.get("name", "")).strip()
            if not name:
                return None
            tool_input = action.get("input", "")
            return thought, name, {"input": "" if tool_input is None else tool_input}
        return None

    @staticmethod
    def _extract_json_object(text: str) -> Optional[str]:
        """从文本抽取第一个平衡的 JSON 对象。"""
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
            cleaned = re.sub(r"```$", "", cleaned).strip()
        start = cleaned.find("{")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        collected: list[str] = []
        for char in cleaned[start:]:
            collected.append(char)
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return "".join(collected).strip()
        return None

    def build_additional_context(self, input_text: str) -> str:
        """兼容旧接口占位（新内核用 build_system_prompt 注入 system 消息）。"""
        return ""
