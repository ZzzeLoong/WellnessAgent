"""Business-specific wellness planning agent."""

import importlib
from datetime import datetime
from pathlib import Path
import sys
from typing import Any, Iterable, Optional

from .prompts import WELLNESS_REACT_PROMPT_TEMPLATE
from .schemas import WellnessProfile
from .service import WellnessAgentService


SUPPORTED_TOOL_GROUPS = frozenset({"memory", "rag"})
DEFAULT_TOOL_GROUPS = frozenset({"memory", "rag"})

try:
    from ..agents.react_agent import ReActAgent
    from ..core.llm import HelloAgentsLLM
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
            custom_prompt=custom_prompt or WELLNESS_REACT_PROMPT_TEMPLATE,
        )

        self.service = WellnessAgentService(
            memory_tool=self.memory_tool,
            rag_tool=self.rag_tool,
            knowledgebase_dir=Path(__file__).resolve().parent / "knowledgebase" / "raw",
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
                "更新当前用户画像。输入格式：field=value;field=value。若要清空字段，可写 field=none 或 field=无",
                self._profile_set,
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

    def _profile_set(self, input_text: str) -> str:
        """Update structured profile fields from a compact text format."""
        updates = self._parse_profile_updates(input_text)
        return self.service.profile_set(updates)

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

    def build_additional_context(self, input_text: str) -> str:
        """Inject profile, recent dialogue, and active working-memory summaries.

        For memory-disabled baselines we still expose the rolling dialogue window
        (it comes from the base ReActAgent message history, not from the memory
        system), so the agent can always reason about prior turns.
        """
        sections: list[str] = []

        if self._has_group("memory"):
            sections.extend(
                [
                    "### 当前用户画像摘要",
                    self.service.get_current_profile_summary(),
                    "",
                    "### 补充长期记忆摘要",
                    self.service.get_distilled_memory_summary(limit=4),
                    "",
                ]
            )

        sections.extend(
            [
                "### 最近对话窗口",
                self._format_recent_dialogue_window(),
            ]
        )

        if self._has_group("memory"):
            sections.extend(
                [
                    "",
                    "### 当前有效短期上下文",
                    self.service.get_working_context_summary(limit=6, max_chars=900),
                ]
            )

        return "\n".join(sections)

    def chat(self, user_input: str, **kwargs) -> str:
        """Run one planning turn with automatic session-context injection."""
        answer = self.run(user_input, **kwargs)
        self._handle_post_turn_memory()
        return answer

    def chat_with_trace(self, user_input: str, **kwargs) -> dict[str, Any]:
        """Run one planning turn and return structured trace data."""
        result = self.run_with_trace(user_input, **kwargs)
        self._handle_post_turn_memory()
        state = self.get_state_dict()
        return {
            "answer": result.final_answer,
            "terminated_reason": result.terminated_reason,
            "steps": [step.to_dict() for step in result.steps],
            "state": state,
        }

    def get_state_dict(self) -> dict[str, Any]:
        """Expose structured memory and RAG state."""
        state = self.service.get_state_dict()
        state["recent_dialogue_window"] = self._format_recent_dialogue_window()
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
        self.clear_history()
        return self.service.clear_user_memories()

    def cleanup(self) -> None:
        """Release session-scoped memory state."""
        self.memory_tool.clear_session()

    def _format_recent_dialogue_window(self, max_messages: int = 6) -> str:
        """Format a sliding window of recent dialogue turns for prompt injection."""
        history = self.get_history()
        if not history:
            return "当前没有历史对话。"

        recent_messages = history[-max_messages:]
        lines: list[str] = []
        for message in recent_messages:
            if message.role not in {"user", "assistant"}:
                continue
            role_name = "用户" if message.role == "user" else "助手"
            content = " ".join(message.content.split())
            if len(content) > 220:
                content = f"{content[:217].rstrip()}..."
            lines.append(f"- {role_name}: {content}")
        return "\n".join(lines) if lines else "当前没有历史对话。"

    def _get_completed_turns(self) -> list[dict[str, str]]:
        """Reconstruct completed user/assistant turns from message history."""
        turns: list[dict[str, str]] = []
        pending_user: str | None = None
        for message in self.get_history():
            if message.role == "user":
                pending_user = message.content
            elif message.role == "assistant" and pending_user is not None:
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
