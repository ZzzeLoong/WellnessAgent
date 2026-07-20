"""Business-specific wellness planning agent."""

import importlib
import os
from datetime import datetime
from pathlib import Path
import sys
from typing import Any, Iterable, Optional

from .prompts import WELLNESS_FALLBACK_JSON_INSTRUCTIONS, WELLNESS_SYSTEM_PROMPT
from .guardrails import DietGuardrails
from .schemas import WellnessProfile
from .service import WellnessAgentService
from .orchestrator import Orchestrator, OrchestrationContext, ROUTE_SIMPLE
from .subagents import make_subagent_factory
from . import safety_rules
from ..core.hitl import (
    ConfirmationDecision,
    DECISION_APPROVE,
    DECISION_MODIFY,
    DECISION_REJECT,
    KIND_PROFILE_UPDATE,
    KIND_SAFETY_RISK,
    PendingConfirmation,
)


SUPPORTED_TOOL_GROUPS = frozenset({"memory", "rag"})
DEFAULT_TOOL_GROUPS = frozenset({"memory", "rag"})

try:
    from ..agents.react_agent import ReActAgent
    from ..core.llm import HelloAgentsLLM
    from ..core.session_store import SessionStore
    from ..observability.trace_logger import TraceLogger
    from ..memory import MemoryConfig
    from ..tools.builtin.memory_tool import MemoryTool
    from ..tools.builtin.rag_tool import RAGTool
    from ..tools.registry import ToolRegistry
except ImportError:
    repo_root = Path(__file__).resolve().parents[1]
    repo_parent = repo_root.parent
    if str(repo_parent) not in sys.path:
        sys.path.insert(0, str(repo_parent))
    package_name = repo_root.name

    react_agent_module = importlib.import_module(f"{package_name}.agents.react_agent")
    llm_module = importlib.import_module(f"{package_name}.core.llm")
    session_store_module = importlib.import_module(f"{package_name}.core.session_store")
    trace_logger_module = importlib.import_module(
        f"{package_name}.observability.trace_logger"
    )
    memory_module = importlib.import_module(f"{package_name}.memory")
    memory_tool_module = importlib.import_module(
        f"{package_name}.tools.builtin.memory_tool"
    )
    rag_tool_module = importlib.import_module(
        f"{package_name}.tools.builtin.rag_tool"
    )
    registry_module = importlib.import_module(f"{package_name}.tools.registry")

    ReActAgent = react_agent_module.ReActAgent
    HelloAgentsLLM = llm_module.HelloAgentsLLM
    SessionStore = session_store_module.SessionStore
    TraceLogger = trace_logger_module.TraceLogger
    MemoryConfig = memory_module.MemoryConfig
    MemoryTool = memory_tool_module.MemoryTool
    RAGTool = rag_tool_module.RAGTool
    ToolRegistry = registry_module.ToolRegistry


class WellnessPlanningAgent(ReActAgent):
    """A ReAct-based nutrition and wellness planning agent."""

    def __init__(
        self,
        user_id: str,
        name: str = "WellnessPlanner",
        llm: Optional[HelloAgentsLLM] = None,
        memory_config: Optional[MemoryConfig] = None,
        knowledge_base_path: Optional[str] = None,
        rag_namespace: Optional[str] = None,
        max_steps: int = 8,
        custom_prompt: Optional[str] = None,
        tool_groups: Optional[Iterable[str]] = None,
    ):
        self.user_id = user_id
        self.tool_groups = self._normalize_tool_groups(tool_groups)
        self.memory_tool = MemoryTool(
            user_id=user_id,
            memory_config=memory_config or MemoryConfig(
                working_memory_capacity=24,
                importance_threshold=0.25,
                decay_factor=0.96,
            ),
            memory_types=["working", "episodic"],
        )
        self.rag_tool = RAGTool(
            knowledge_base_path=knowledge_base_path or str(
                Path(__file__).resolve().parent / "knowledgebase" / "store" / user_id
            ),
            rag_namespace=rag_namespace or f"wellness_{user_id}",
        )

        tool_registry = ToolRegistry()
        self._register_tool_functions(tool_registry)

        super().__init__(
            name=name,
            llm=llm or HelloAgentsLLM(),
            tool_registry=tool_registry,
            max_steps=max_steps,
            system_prompt=custom_prompt or WELLNESS_SYSTEM_PROMPT,
            fallback_prompt_suffix=WELLNESS_FALLBACK_JSON_INSTRUCTIONS,
        )

        self.service = WellnessAgentService(
            memory_tool=self.memory_tool,
            rag_tool=self.rag_tool,
            knowledgebase_dir=Path(__file__).resolve().parent / "knowledgebase" / "raw",
        )

        # 会话持久化（R17）。
        self.session_store = SessionStore()
        self.session_id: Optional[str] = None

        # 装配新内核的钩子：注入 system 消息、Guardrails、上下文摘要 LLM。
        self.build_system_prompt = self._build_system_prompt
        self.get_guardrail_profile = self.service.get_current_profile
        self.get_summary_llm = self.service._get_distill_llm
        self.guardrails = DietGuardrails(llm=self._maybe_guardrail_llm())

        # Trace（R5）。默认按 user_id 落盘，session_id 在开启会话时确定。
        self._trace_enabled = (
            os.getenv("WELLNESS_TRACE_ENABLED", "true").lower() == "true"
        )

        # 编排层（R6）。总开关关闭时永远走一期单体路径（simple 等价）。
        self._orchestration_enabled = (
            os.getenv("WELLNESS_ORCHESTRATION_ENABLED", "true").lower() == "true"
        )
        # HITL 确认总开关（R7）。
        self._hitl_enabled = (
            os.getenv("WELLNESS_HITL_ENABLED", "true").lower() == "true"
        )
        # 当前待确认项（挂起态）。落盘在 SessionStore.metadata，运行时缓存于此。
        self._pending_confirmation: Optional[PendingConfirmation] = None
        subagent_max_steps = int(os.getenv("WELLNESS_SUBAGENT_MAX_STEPS", "6"))
        # 子代理复用主 agent 的 llm / tool_registry / 只读 service。
        self.orchestrator = Orchestrator(
            llm=self.llm,
            subagent_factory=make_subagent_factory(
                llm=self.llm,
                base_registry=self.tool_registry,
                service=self.service,
                max_steps=subagent_max_steps,
            ),
            service=self.service,
            hitl_enabled=self._hitl_enabled,
        )

    @staticmethod
    def _normalize_tool_groups(tool_groups: Optional[Iterable[str]]) -> frozenset[str]:
        """Validate and normalize the requested tool groups."""
        if tool_groups is None:
            return DEFAULT_TOOL_GROUPS
        normalized: set[str] = set()
        for raw in tool_groups:
            value = (raw or "").strip().lower()
            if not value:
                continue
            if value not in SUPPORTED_TOOL_GROUPS:
                raise ValueError(
                    f"Unsupported tool group: {value}. "
                    f"Allowed: {sorted(SUPPORTED_TOOL_GROUPS)}"
                )
            normalized.add(value)
        return frozenset(normalized)

    def _has_group(self, group: str) -> bool:
        """Return True when a tool group is enabled for this agent instance."""
        return group in self.tool_groups

    def _register_tool_functions(self, tool_registry: ToolRegistry) -> None:
        """Register string-based wrappers compatible with the current ReAct loop."""
        if self._has_group("memory"):
            tool_registry.register_function(
                "profile_get",
                "读取当前用户画像。输入留空即可。",
                self._profile_get,
            )
            tool_registry.register_function(
                "profile_set",
                "更新当前用户画像。仅写入稳定的长期信息；list 字段传字符串数组，其余传字符串。清空字段可传空数组或空串。",
                self._profile_set,
                parameters=[
                    {"name": "allergies", "type": "array", "items": "string", "description": "过敏原（长期）。"},
                    {"name": "dislikes", "type": "array", "items": "string", "description": "长期忌口。"},
                    {"name": "medical_notes", "type": "array", "items": "string", "description": "医疗备注（长期）。"},
                    {"name": "preferred_cuisines", "type": "array", "items": "string", "description": "长期偏好菜系/风味。"},
                    {"name": "cooking_constraints", "type": "array", "items": "string", "description": "稳定的做饭/备餐限制。"},
                    {"name": "diet_pattern", "type": "string", "description": "饮食模式或长期饮食限制。"},
                    {"name": "goal", "type": "string", "description": "长期营养目标。"},
                    {"name": "notes", "type": "string", "description": "其他稳定的长期提醒。"},
                ],
            )
            tool_registry.register_function(
                "session_note",
                "写入当前会话的短期上下文。输入为一段应暂时记住的文本。",
                self._session_note,
            )
            tool_registry.register_function(
                "session_recall",
                "检索当前会话短期上下文。输入为查询内容。",
                self._session_recall,
            )
            tool_registry.register_function(
                "session_digest",
                "查看当前会话 working memory 摘要。输入留空即可。",
                self._session_digest,
            )
            tool_registry.register_function(
                "memory_search",
                "检索长期用户记忆。输入为查询内容。",
                self._memory_search,
            )
            tool_registry.register_function(
                "memory_remember",
                "写入非结构化长期规则或长期反馈。输入为要长期记住的文本。",
                self._memory_remember,
            )
            tool_registry.register_function(
                "memory_digest",
                "查看长期记忆高层摘要。输入留空即可。",
                self._memory_digest,
            )
        if self._has_group("rag"):
            tool_registry.register_function(
                "kb_search",
                "检索营养知识库片段。输入为查询内容。",
                self._kb_search,
            )
            tool_registry.register_function(
                "kb_answer",
                "基于营养知识库生成回答。输入为问题。",
                self._kb_answer,
            )
            tool_registry.register_function(
                "kb_status",
                "查看当前知识库 namespace 状态。输入留空即可。",
                self._kb_status,
            )

    def _profile_get(self, _input_text: str) -> str:
        """Read the current structured profile."""
        return self.service.profile_get()

    def _profile_set(self, input_text: str | None = None, **fields: Any) -> str:
        """Update structured profile fields.

        支持两种入参：
        - FC 结构化多参数：``allergies=[...], diet_pattern="..."`` 等关键字。
        - 回退单参数：``input="field=value;field=value"`` 兼容旧格式。
        """
        if fields:
            updates = self._normalize_structured_updates(fields)
            self.service.record_profile_parse_debug(str(fields), updates)
            return self.service.profile_set(updates)
        updates = self._parse_profile_updates(input_text or "")
        return self.service.profile_set(updates)

    def _normalize_structured_updates(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Normalize FC-provided structured profile fields."""
        updates: dict[str, Any] = {}
        for field_name, value in fields.items():
            normalized_field = field_name.strip().lower()
            if normalized_field not in WellnessProfile.SUPPORTED_FIELDS:
                continue
            if normalized_field in WellnessProfile.LIST_FIELDS:
                updates[normalized_field] = WellnessProfile._normalize_list(value)
            else:
                updates[normalized_field] = WellnessProfile._normalize_text(value)
        return updates

    def _session_note(self, input_text: str) -> str:
        """Store short-lived session context."""
        return self.service.session_note(input_text.strip())

    def _session_recall(self, input_text: str) -> str:
        """Retrieve short-lived session context."""
        return self.service.session_recall(input_text.strip(), limit=5)

    def _session_digest(self, _input_text: str) -> str:
        """Summarize current session working memory."""
        return self.service.session_digest(limit=5)

    def _memory_search(self, input_text: str) -> str:
        """Retrieve long-term user memory."""
        return self.service.memory_search(input_text.strip(), limit=5)

    def _memory_remember(self, input_text: str) -> str:
        """Store long-term non-structured memory."""
        return self.service.memory_remember(input_text.strip(), importance=0.9)

    def _memory_digest(self, _input_text: str) -> str:
        """Summarize long-term memory state."""
        return self.service.memory_digest(limit=10)

    def _kb_search(self, input_text: str) -> str:
        """Search the nutrition knowledge base."""
        return self.service.kb_search(input_text.strip(), limit=5)

    def _kb_answer(self, input_text: str) -> str:
        """Answer with the nutrition knowledge base."""
        return self.service.kb_answer(input_text.strip(), limit=5)

    def _kb_status(self, _input_text: str) -> str:
        """Return KB stats."""
        return self.service.kb_status()

    def _parse_profile_updates(self, payload: str) -> dict[str, Any]:
        """Parse `field=value;field=value` updates for profile_set."""
        updates: dict[str, Any] = {}
        for chunk in payload.split(";"):
            part = chunk.strip()
            if not part or "=" not in part:
                continue
            field_name, raw_value = part.split("=", 1)
            normalized_field = field_name.strip().lower()
            if normalized_field not in WellnessProfile.SUPPORTED_FIELDS:
                continue
            value = raw_value.strip()
            if normalized_field in WellnessProfile.LIST_FIELDS:
                updates[normalized_field] = WellnessProfile._normalize_list(value)
            else:
                updates[normalized_field] = WellnessProfile._normalize_text(value)
        self.service.record_profile_parse_debug(payload, updates)
        return updates

    def onboard_user(self, profile: WellnessProfile) -> str:
        """Save a structured user profile into long-lived memory."""
        return self.service.seed_profile(profile)

    def seed_knowledge_base(self) -> list[str]:
        """Load a small default nutrition knowledge base."""
        return self.service.seed_default_knowledge()

    def _build_system_prompt(self, input_text: str) -> str:
        """Compose the per-turn system message.

        新内核把画像/长期记忆/working 摘要注入到 **system 消息**（每轮刷新一次），
        对话历史交给累积 messages 数组承担，因此这里不再注入对话滑窗（边界规则 2）。
        """
        sections: list[str] = [WELLNESS_SYSTEM_PROMPT]

        if self._has_group("memory"):
            sections.extend(
                [
                    "",
                    "## 系统已注入上下文",
                    "### 当前用户画像摘要",
                    self.service.get_current_profile_summary(),
                    "",
                    "### 补充长期记忆摘要",
                    self.service.get_distilled_memory_summary(limit=4),
                    "",
                    "### 当前有效短期上下文",
                    self.service.get_working_context_summary(limit=6, max_chars=900),
                ]
            )
        return "\n".join(sections)

    def build_additional_context(self, input_text: str) -> str:
        """Deprecated: 旧内核每步注入用；新内核用 _build_system_prompt。保留空实现。"""
        return ""

    def _maybe_guardrail_llm(self):
        """Return an LLM for the guardrails rule_llm layer, only when enabled."""
        mode = os.getenv("WELLNESS_GUARDRAILS_MODE", "rule").strip().lower()
        if mode != "rule_llm":
            return None
        try:
            return self.service._get_distill_llm()
        except Exception:
            return None

    def _ensure_session(self) -> None:
        """Ensure an active session id (对话 session 与 working memory session 统一)."""
        if self.session_id is None:
            self.session_id = (
                self.session_store.latest_session_id(self.user_id)
                or self.session_store.new_session_id()
            )
        # 对话 session 与 working memory session 统一为同一个 id。
        self.memory_tool.current_session_id = self.session_id
        if self._trace_enabled:
            self.trace_logger = TraceLogger(self.user_id, self.session_id)
        else:
            self.trace_logger = None

    def new_session(self) -> str:
        """Start a fresh session: 清空对话历史 + working memory（长期记忆保留）。"""
        self.session_id = self.session_store.new_session_id()
        self.messages = []
        self.clear_history()
        self.memory_tool.clear_session()
        self.memory_tool.current_session_id = self.session_id
        return self.session_id

    def select_session(self, session_id: Optional[str]) -> None:
        """切到指定 session（带 messages 恢复）；session_id 为空则保持/续接最近会话。

        - 与当前 session 相同：无需重载。
        - 指定了已存在的 session：``load_session`` 恢复其 messages。
        - 指定了不存在的 session：以该 id 新建空会话。
        """
        if not session_id or session_id == self.session_id:
            return
        if not self.load_session(session_id):
            # 未落盘：以该 id 起一个空会话。
            self.session_id = session_id
            self.messages = []
            self.clear_history()
            self.memory_tool.clear_session()
            self.memory_tool.current_session_id = session_id

    def chat(self, user_input: str, **kwargs) -> str:
        """Run one planning turn with automatic session-context injection."""
        self._ensure_session()
        answer = self.run(user_input, **kwargs)
        self._handle_post_turn_memory()
        self._persist_session()
        return answer

    def chat_with_trace(
        self, user_input: str, confirmation: dict | None = None, **kwargs
    ) -> dict[str, Any]:
        """Run one planning turn and return structured trace data.

        R6：先经 Orchestrator 分诊。``simple`` 走一期单体原路径（字节级等价）；
        ``composite`` 走编排 pipeline，把聚合答案交给一期 ``_finalize``（复用 guardrails
        硬红线 + 状态写入），对外契约保持一期字段，额外附加 ``orchestration``。

        R7 HITL：若 ``confirmation`` 非空，则本轮是对上一轮挂起项的确认恢复；否则
        composite pipeline 命中高影响步骤时挂起，返回 ``confirmation`` 字段等待下一轮。
        """
        self._ensure_session()

        # R7 恢复：解析确认决策，构造 resume 上下文（含落库画像 / 决策）。
        resume_ctx: dict | None = None
        if confirmation:
            resume_ctx = self._resume_from_confirmation(confirmation)

        orchestration_meta: dict[str, Any] | None = None
        confirmation_out: dict[str, Any] | None = None
        if self._orchestration_enabled:
            self.orchestrator.trace_logger = self.trace_logger
            ctx = self._build_orchestration_context(user_input)
            orch = self.orchestrator.handle(user_input, ctx, resume=resume_ctx)
            if orch.route != ROUTE_SIMPLE:
                orchestration_meta = orch.to_dict()
                if orch.pending is not None:
                    # 挂起：写 SessionStore 挂起态，返回 confirmation，本回合结束。
                    return self._suspend_for_confirmation(user_input, orch)
                result = self._finalize_composite(user_input, orch.answer or "")
                return self._build_chat_response(
                    result, orchestration_meta, confirmation_out
                )

        # simple 路径（或编排关闭）：一期单体原路径，完全不变。
        result = self.run_with_trace(user_input, **kwargs)
        if self._orchestration_enabled:
            orchestration_meta = {"route": ROUTE_SIMPLE, "delegate_to_monolith": True}
        return self._build_chat_response(result, orchestration_meta, confirmation_out)

    def _build_chat_response(
        self,
        result,
        orchestration_meta: dict[str, Any] | None,
        confirmation_out: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """收尾（记忆/落盘/trace）并组装对外响应 dict。"""
        self._handle_post_turn_memory()
        self._persist_session()
        if self.trace_logger is not None:
            self.trace_logger.finalize()
        response: dict[str, Any] = {
            "answer": result.final_answer,
            "terminated_reason": result.terminated_reason,
            "steps": [step.to_dict() for step in result.steps],
            "state": self.get_state_dict(),
            "session_id": self.session_id,
            "trace_id": self.session_id if self.trace_logger is not None else None,
        }
        if orchestration_meta is not None:
            response["orchestration"] = orchestration_meta
        if confirmation_out is not None:
            response["confirmation"] = confirmation_out
        return response

    # ------------------------------------------------------------------ HITL
    def _suspend_for_confirmation(self, user_input: str, orch) -> dict[str, Any]:
        """命中高影响步骤：持久化挂起态，返回带 confirmation 的响应（回合正常结束）。

        方案 §3.4：本回合 ``terminated_reason`` 内部标记 ``awaiting_confirmation``，
        对外映射为 ``finished``；把 ``PendingConfirmation`` 写入 ``SessionStore``
        的 ``metadata["pending_confirmation"]``。答案取确认提示文本（不经 guardrails
        改写，因其本身是系统提示）。
        """
        from ..agents.react_agent import TERMINATED_FINISHED
        from ..core.message import Message

        pending = orch.pending
        self._pending_confirmation = pending

        # 把本轮 user 与确认提示写进主 messages（回合闭环、可续接）。
        self._refresh_system_message(user_input)
        self._append_message(Message(user_input, "user"))
        self._append_message(Message(pending.prompt, "assistant"))
        if self.trace_logger is not None:
            self.trace_logger.log_event(
                "message_written", {"role": "user", "content": user_input}
            )

        self._handle_post_turn_memory()
        self._persist_session(pending=pending)
        if self.trace_logger is not None:
            self.trace_logger.finalize()

        return {
            "answer": pending.prompt,
            # 对外仍为 finished；awaiting_confirmation 仅作内部/trace 标记（方案 §6.1）。
            "terminated_reason": TERMINATED_FINISHED,
            "steps": [],
            "state": self.get_state_dict(),
            "session_id": self.session_id,
            "trace_id": self.session_id if self.trace_logger is not None else None,
            "orchestration": orch.to_dict(),
            "confirmation": pending.public_view(),
        }

    def _resume_from_confirmation(self, confirmation: dict) -> dict | None:
        """解析并执行用户确认，返回给 orchestrator 的 resume 上下文。

        方案 §3.4：校验 ``confirm_id`` 与挂起态匹配；approve 执行动作（如
        ``profile_set``）、modify 用 patch 覆盖后执行、reject 丢弃。清除挂起态并写
        ``confirm_resume`` trace。不匹配/过期则忽略（返回 None，按新请求处理）。
        """
        decision = ConfirmationDecision.from_dict(confirmation)
        pending = self._load_pending_confirmation()
        if decision is None or pending is None:
            return None
        if decision.confirm_id != pending.confirm_id:
            # 不匹配/过期：忽略确认，按新请求处理。
            return None

        applied: dict[str, Any] = {}
        note = ""
        if decision.decision == DECISION_REJECT:
            note = "用户拒绝了该建议，按原约束继续。"
        elif pending.kind == KIND_PROFILE_UPDATE:
            updates = dict(pending.payload.get("suggested_updates", {}) or {})
            if decision.decision == DECISION_MODIFY and decision.patch:
                updates.update(decision.patch)
            if updates:
                # 敏感画像变更由主 agent 落库（长期记忆不可逆变更不下放子代理）。
                try:
                    normalized = self._normalize_structured_updates(updates)
                    if normalized:
                        self.service.profile_set(normalized)
                        applied = normalized
                except Exception:
                    applied = {}
        elif pending.kind == KIND_SAFETY_RISK:
            if decision.decision == DECISION_MODIFY:
                note = str(decision.patch.get("note", "")) if decision.patch else ""
                applied = {"accepted": "modified"}
            else:  # approve
                applied = {"accepted": "safe_alternative", "advice": pending.payload.get("advice")}

        if self.trace_logger is not None:
            self.trace_logger.log_event(
                "confirm_resume",
                {"confirm_id": pending.confirm_id, "decision": decision.decision},
            )
        # 清除挂起态。
        self._pending_confirmation = None
        return {
            "decision": decision.decision,
            "kind": pending.kind,
            "applied": applied,
            "note": note,
        }

    def _load_pending_confirmation(self) -> PendingConfirmation | None:
        """从运行时缓存或 SessionStore.metadata 加载挂起态。"""
        if self._pending_confirmation is not None:
            return self._pending_confirmation
        if self.session_id is None:
            return None
        try:
            data = self.session_store.load(self.user_id, self.session_id)
        except Exception:
            return None
        if not data:
            return None
        raw = (data.get("metadata") or {}).get("pending_confirmation")
        return PendingConfirmation.from_dict(raw)

    def _build_orchestration_context(self, user_input: str) -> OrchestrationContext:
        """装配编排上下文：注入只读的用户画像风险词，供 safety 子代理使用。"""
        risk_terms: list[str] = []
        try:
            profile = self.service.get_current_profile()
            risk_terms = safety_rules.collect_risk_terms(profile)
        except Exception:
            risk_terms = []
        return OrchestrationContext(message=user_input, risk_terms=risk_terms)

    def _finalize_composite(self, user_input: str, answer: str):
        """把 composite 聚合答案并入主 messages，走一期 _finalize（guardrails + 状态）。

        与 simple 路径保持一致：主 messages 追加本轮 user + 经 guardrails 复核的
        assistant 文本，返回结构化 ReActRunResult。编排子代理的完整历史**不**灌回主
        messages（防膨胀，呼应 R15），只并入最终聚合答案。
        """
        from ..agents.react_agent import TERMINATED_FINISHED
        from ..core.message import Message

        self._refresh_system_message(user_input)
        self._append_message(Message(user_input, "user"))
        if self.trace_logger is not None:
            self.trace_logger.log_event(
                "message_written", {"role": "user", "content": user_input}
            )
        # 复用一期 _finalize：guardrails 硬红线复核 + 把 safe_answer 追加进 messages。
        return self._finalize(user_input, answer, [], TERMINATED_FINISHED)

    def chat_stream(self, user_input: str, confirmation: dict | None = None, **kwargs):
        """流式对话（R2）：逐 step/tool 事件 + 最终答案分段 ``LLM_CHUNK``。

        方案 §6.2 一期做法：不做逐 token，final_answer 由本层按块切片输出。
        产出 :class:`StreamEvent`，供 ``/api/chat/stream`` 序列化为 SSE。

        R6/R7：composite 路径下发编排/子代理事件；命中高影响步骤下发 ``CONFIRM``
        并结束本回合（挂起态已落盘，等待下一轮 ``confirmation`` 恢复）。
        """
        from .server.streaming import StreamEvent, StreamEventType

        self._ensure_session()
        yield StreamEvent(
            StreamEventType.AGENT_START, {"session_id": self.session_id}
        )

        # R6/R7 编排路径：先分诊，composite 走编排（可能挂起）。
        if self._orchestration_enabled:
            self.orchestrator.trace_logger = self.trace_logger
            resume_ctx = self._resume_from_confirmation(confirmation) if confirmation else None
            ctx = self._build_orchestration_context(user_input)
            orch = self.orchestrator.handle(user_input, ctx, resume=resume_ctx)
            yield StreamEvent(
                StreamEventType.ORCHESTRATOR_TRIAGE,
                {"route": orch.route, "reason": orch.reason},
            )
            if orch.route != ROUTE_SIMPLE:
                yield from self._stream_composite(user_input, orch)
                return

        final_answer = ""
        terminated_reason = "finished"
        safety: dict[str, Any] | None = None
        steps: list[dict] = []
        try:
            for event in self.stream_run(user_input, **kwargs):
                etype = event.get("type")
                if etype == "step_start":
                    yield StreamEvent(
                        StreamEventType.STEP_START, {"step": event["step"]}
                    )
                elif etype == "thinking":
                    yield StreamEvent(
                        StreamEventType.THINKING,
                        {"step": event["step"], "content": event.get("content", "")},
                    )
                elif etype == "tool_call":
                    yield StreamEvent(
                        StreamEventType.TOOL_CALL_START,
                        {
                            "step": event["step"],
                            "id": event.get("id"),
                            "name": event.get("name"),
                            "arguments": event.get("arguments"),
                        },
                    )
                elif etype == "tool_result":
                    yield StreamEvent(
                        StreamEventType.TOOL_CALL_FINISH,
                        {
                            "step": event["step"],
                            "tool_call_id": event.get("tool_call_id"),
                            "name": event.get("name"),
                            "status": event.get("status"),
                            "content": event.get("content"),
                        },
                    )
                elif etype == "step":
                    steps.append(event["record"].to_dict())
                elif etype == "error":
                    yield StreamEvent(
                        StreamEventType.ERROR,
                        {"step": event.get("step"), "message": event.get("message")},
                    )
                elif etype == "result":
                    result = event["result"]
                    final_answer = result.final_answer
                    terminated_reason = result.terminated_reason
        except Exception as exc:  # noqa: BLE001
            yield StreamEvent(StreamEventType.ERROR, {"message": str(exc)})
            return

        # 最终答案分段（一期：按固定块切片模拟增量）。
        for chunk in self._chunk_text(final_answer):
            yield StreamEvent(StreamEventType.LLM_CHUNK, {"delta": chunk})

        # 收尾：记忆/落盘/trace（与非流式一致）。
        self._handle_post_turn_memory()
        self._persist_session()
        if self.trace_logger is not None:
            self.trace_logger.finalize()

        yield StreamEvent(
            StreamEventType.AGENT_FINISH,
            {
                "answer": final_answer,
                "terminated_reason": terminated_reason,
                "session_id": self.session_id,
                "trace_id": self.session_id if self.trace_logger is not None else None,
                "state": self.get_state_dict(),
                "steps": steps,
                "safety": safety,
            },
        )

    def _stream_composite(self, user_input: str, orch):
        """把编排结果转成流事件：子代理摘要 → (CONFIRM | 聚合答案分段) → finish。"""
        from .server.streaming import StreamEvent, StreamEventType

        # 子代理结果事件（编排是同步跑完的，这里补发给前端可视化）。
        for brief in orch.subagent_results:
            yield StreamEvent(
                StreamEventType.SUBAGENT_START,
                {"subagent": brief.get("name"), "tools_allowed": brief.get("tools_used")},
            )
            yield StreamEvent(
                StreamEventType.SUBAGENT_RESULT,
                {
                    "subagent": brief.get("name"),
                    "success": brief.get("success"),
                    "summary": brief.get("summary"),
                    "steps": brief.get("steps"),
                    "duration_ms": brief.get("duration_ms"),
                },
            )

        # 命中高影响步骤：下发 CONFIRM，挂起本回合。
        if orch.pending is not None:
            pending = orch.pending
            self._pending_confirmation = pending
            from ..core.message import Message

            self._refresh_system_message(user_input)
            self._append_message(Message(user_input, "user"))
            self._append_message(Message(pending.prompt, "assistant"))
            self._handle_post_turn_memory()
            self._persist_session(pending=pending)
            if self.trace_logger is not None:
                self.trace_logger.finalize()
            yield StreamEvent(StreamEventType.CONFIRM, pending.public_view())
            yield StreamEvent(
                StreamEventType.AGENT_FINISH,
                {
                    "answer": pending.prompt,
                    "terminated_reason": "finished",
                    "session_id": self.session_id,
                    "trace_id": self.session_id if self.trace_logger is not None else None,
                    "state": self.get_state_dict(),
                    "steps": [],
                    "safety": None,
                    "orchestration": orch.to_dict(),
                    "confirmation": pending.public_view(),
                },
            )
            return

        # 无挂起：聚合答案经 guardrails 复核，分段下发。
        yield StreamEvent(
            StreamEventType.ORCHESTRATOR_AGGREGATE,
            {
                "subagents": [b.get("name") for b in orch.subagent_results],
                "answer_preview": (orch.answer or "")[:200],
            },
        )
        result = self._finalize_composite(user_input, orch.answer or "")
        final_answer = result.final_answer
        for chunk in self._chunk_text(final_answer):
            yield StreamEvent(StreamEventType.LLM_CHUNK, {"delta": chunk})

        self._handle_post_turn_memory()
        self._persist_session()
        if self.trace_logger is not None:
            self.trace_logger.finalize()
        yield StreamEvent(
            StreamEventType.AGENT_FINISH,
            {
                "answer": final_answer,
                "terminated_reason": result.terminated_reason,
                "session_id": self.session_id,
                "trace_id": self.session_id if self.trace_logger is not None else None,
                "state": self.get_state_dict(),
                "steps": [s.to_dict() for s in result.steps],
                "safety": None,
                "orchestration": orch.to_dict(),
            },
        )

    @staticmethod
    def _chunk_text(text: str, size: int = 48) -> list[str]:
        """把最终答案切成若干块，模拟逐步渲染（一期做法）。"""
        if not text:
            return []
        return [text[i : i + size] for i in range(0, len(text), size)]

    def _persist_session(self, pending: PendingConfirmation | None = None) -> None:
        """Persist accumulated messages for the current session (R17).

        R7：``pending`` 非空时把挂起态写入 ``metadata["pending_confirmation"]``；为空
        时清除该键（确认恢复后不再残留挂起态）。消息序列化格式不变（方案 §0.2）。
        """
        if self.session_id is None:
            return
        metadata: dict[str, Any] = {"agent": self.name}
        if pending is not None:
            metadata["pending_confirmation"] = pending.to_dict()
        try:
            self.session_store.save(
                self.user_id,
                self.session_id,
                self.messages,
                metadata=metadata,
            )
        except Exception:
            pass

    def load_session(self, session_id: str) -> bool:
        """Restore accumulated messages from a persisted session."""
        data = self.session_store.load(self.user_id, session_id)
        if not data:
            return False
        self.session_id = session_id
        self.messages = list(data["messages"])
        self.history_manager.set_history(self.messages)
        self.memory_tool.current_session_id = session_id
        return True

    def get_state_dict(self) -> dict[str, Any]:
        """Expose structured memory and RAG state."""
        state = self.service.get_state_dict()
        state["recent_dialogue_window"] = self._recent_dialogue_window()
        state["completed_turn_count"] = len(self._get_completed_turns())
        return state

    def get_state_summary(self) -> str:
        """Expose current memory and RAG status for debugging."""
        return self.service.get_state_summary()

    def list_knowledgebase_files(self) -> list[dict[str, Any]]:
        """Return raw knowledgebase file metadata."""
        return self.service.list_knowledgebase_files()

    def read_knowledgebase_file(self, name: str) -> dict[str, Any] | None:
        """Read a raw knowledgebase markdown file."""
        return self.service.read_knowledgebase_file(name)

    def clear_user_memories(self) -> dict[str, str]:
        """Clear only the current user's memories."""
        # P0-2：回合派生已收敛到 ``self.messages``，清记忆时一并清空对话消息。
        self.messages = []
        self.history_manager.set_history(self.messages)
        self.clear_history()
        return self.service.clear_user_memories()

    def cleanup(self) -> None:
        """Persist session then release session-scoped memory state."""
        self._persist_session()
        self.memory_tool.clear_session()

    def _recent_dialogue_window(self, max_turns: int = 3) -> str:
        """Render a read-only recent-dialogue snapshot for state/debug display.

        方案边界规则 2：对话历史只由累积 messages 数组承担，不再维护独立的手工滑窗，
        也不注入 prompt。此处仅从 ``_get_completed_turns()``（同样从 messages 派生）
        取最近几轮，供 ``get_state_dict`` 的状态展示/调试使用。
        """
        turns = self._get_completed_turns()
        if not turns:
            return "当前没有历史对话。"

        lines: list[str] = []
        for turn in turns[-max_turns:]:
            for role_name, key in (("用户", "user"), ("助手", "assistant")):
                content = " ".join((turn.get(key) or "").split())
                if not content:
                    continue
                if len(content) > 220:
                    content = f"{content[:217].rstrip()}..."
                lines.append(f"- {role_name}: {content}")
        return "\n".join(lines) if lines else "当前没有历史对话。"

    def _get_completed_turns(self) -> list[dict[str, str]]:
        """Reconstruct completed user/assistant turns from ``self.messages``.

        P0-2：对话历史收敛为累积 ``self.messages`` 单一源（不再依赖基类 ``_history``）。
        ``messages`` 首条为 system（跳过）；一轮内可能有多条 tool_calls-only 的
        assistant 与 tool 消息（无面向用户文本，跳过），回合的最终答案由 ``_finalize``
        补记为一条含文本的 assistant 消息。这里按"user → 其后首条含非空 content 的
        assistant"配对，得到已完成回合。
        """
        turns: list[dict[str, str]] = []
        pending_user: str | None = None
        for message in self.messages:
            if message.role == "user":
                pending_user = message.content
            elif message.role == "assistant" and pending_user is not None:
                content = (message.content or "").strip()
                if not content:
                    # tool_calls-only 的 assistant（无面向用户文本）不算回合收尾。
                    continue
                turns.append({"user": pending_user, "assistant": message.content})
                pending_user = None
        return turns

    def _handle_post_turn_memory(self) -> None:
        """Trigger batch long-term distillation after a completed dialogue turn."""
        completed_turns = self._get_completed_turns()
        if self.memory_tool.current_session_id is None:
            self.memory_tool.current_session_id = (
                f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
        self.memory_tool.conversation_count = len(completed_turns)
        self.service.maybe_distill_turns(completed_turns)
