"""Service helpers for profile memory and RAG knowledge bootstrapping."""

import importlib
import json
import os
import re
from datetime import datetime
from typing import Any
from pathlib import Path
import sys

from .schemas import WellnessProfile

try:
    from ..core.llm import HelloAgentsLLM
    from ..tools.builtin.memory_tool import MemoryTool
    from ..tools.builtin.rag_tool import RAGTool
except ImportError:
    repo_root = Path(__file__).resolve().parents[1]
    repo_parent = repo_root.parent
    if str(repo_parent) not in sys.path:
        sys.path.insert(0, str(repo_parent))
    package_name = repo_root.name
    memory_tool_module = importlib.import_module(
        f"{package_name}.tools.builtin.memory_tool"
    )
    rag_tool_module = importlib.import_module(f"{package_name}.tools.builtin.rag_tool")
    llm_module = importlib.import_module(f"{package_name}.core.llm")
    HelloAgentsLLM = llm_module.HelloAgentsLLM
    MemoryTool = memory_tool_module.MemoryTool
    RAGTool = rag_tool_module.RAGTool


DEFAULT_KNOWLEDGEBASE_DIR = Path(__file__).resolve().parent / "knowledgebase" / "raw"


class WellnessAgentService:
    """Wraps business-level memory and knowledge initialization."""

    def __init__(
        self,
        memory_tool: MemoryTool,
        rag_tool: RAGTool,
        knowledgebase_dir: Path | None = None,
    ):
        self.memory_tool = memory_tool
        self.rag_tool = rag_tool
        self.knowledgebase_dir = knowledgebase_dir or DEFAULT_KNOWLEDGEBASE_DIR
        self.distill_enabled = os.getenv("DISTILL_ENABLED", "true").lower() == "true"
        self.distill_every_n_turns = max(1, int(os.getenv("DISTILL_EVERY_N_TURNS", "4")))
        self._last_distilled_turn_count = 0
        self._distill_llm: HelloAgentsLLM | None = None
        self._last_distill_status = "尚未触发长期记忆提纯。"
        self._last_distill_source = "none"
        self._last_distill_raw_preview = ""
        self._last_profile_parse_debug = "尚未解析 profile_set 输入。"

    @property
    def rag_namespace(self) -> str:
        """Return the active RAG namespace."""
        return self.rag_tool.rag_namespace

    def seed_profile(self, profile: WellnessProfile) -> str:
        """Persist a high-importance user profile into episodic memory."""
        saved = self._save_current_profile(profile, importance=1.0)
        return f"✅ 当前用户画像已写入 (ID: {saved['memory_id'][:8]}...)"

    def profile_get(self) -> str:
        """Return the current structured profile summary."""
        profile = self.get_current_profile()
        if profile.is_empty():
            return "当前用户画像为空。"
        return f"当前用户画像：\n{profile.to_summary_text()}"

    def profile_set(self, updates: dict[str, Any]) -> str:
        """Merge updates into the current structured profile."""
        self._last_profile_parse_debug = (
            f"{self._last_profile_parse_debug}\n解析后字段: {sorted(updates.keys()) or '无'}"
        )
        result = self.upsert_current_profile(updates)
        if not result["updated_fields"]:
            return (
                "未解析到有效的画像字段。可用字段："
                "allergies、diet_pattern、goal、dislikes、medical_notes、"
                "preferred_cuisines、cooking_constraints、notes"
            )
        return (
            "✅ 当前用户画像已更新\n"
            f"更新字段: {', '.join(result['updated_fields'])}\n"
            f"{result['profile'].to_summary_text()}"
        )

    def profile_remove(self, fields: list[str]) -> str:
        """Remove fields from the current structured profile."""
        result = self.remove_current_profile_fields(fields)
        if not result["removed_fields"]:
            return (
                "未解析到要移除的画像字段。可用字段："
                "allergies、diet_pattern、goal、dislikes、medical_notes、"
                "preferred_cuisines、cooking_constraints、notes"
            )
        if result["deleted_profile"]:
            return (
                "✅ 当前用户画像中的字段已移除，且画像已为空，因此已删除该画像记忆。\n"
                f"移除字段: {', '.join(result['removed_fields'])}"
            )
        return (
            "✅ 当前用户画像字段已移除\n"
            f"移除字段: {', '.join(result['removed_fields'])}\n"
            f"{result['profile'].to_summary_text()}"
        )

    def record_profile_parse_debug(self, raw_payload: str, updates: dict[str, Any]) -> None:
        """Record raw and parsed profile_set payload for demo debugging."""
        self._last_profile_parse_debug = (
            f"profile_set 原始输入: {raw_payload or '(空)'}\n"
            f"profile_set 解析结果: {updates or {}}"
        )

    def session_note(self, content: str, importance: float = 0.6) -> str:
        """Store short-lived session context in working memory."""
        normalized_content = " ".join((content or "").split())
        if not normalized_content:
            return "⚠️ 未提供可记录的短期上下文。"

        if self.memory_tool.current_session_id is None:
            self.memory_tool.current_session_id = (
                f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )

        metadata = {
            "category": self._infer_working_memory_category(normalized_content),
            "scope": "short_term",
            "source": "session_note",
            "timestamp": datetime.now().isoformat(),
            "session_id": self.memory_tool.current_session_id,
        }
        memory_id = self.memory_tool.memory_manager.add_memory(
            content=normalized_content,
            memory_type="working",
            importance=importance,
            metadata=metadata,
            auto_classify=False,
        )
        return f"✅ 记忆已添加 (ID: {memory_id[:8]}...)"

    def session_recall(self, query: str, limit: int = 5) -> str:
        """Retrieve short-lived session context from working memory."""
        return self.memory_tool.run(
            {
                "action": "search",
                "query": query,
                "memory_type": "working",
                "limit": limit,
            }
        )

    def session_digest(self, limit: int = 5) -> str:
        """Summarize working-memory-only state for the current session."""
        results = self.get_recent_working_memories(limit=max(limit, 8))
        if not results:
            return "当前 session working memory 为空。"

        sections = self._build_working_memory_sections(results)
        return self._format_working_memory_sections(
            title="当前 session 短期上下文摘要：",
            sections=sections,
            max_chars=1400,
        )

    def get_working_context_summary(self, limit: int = 6, max_chars: int = 800) -> str:
        """Return a prompt-friendly summary of currently active working memory."""
        results = self.get_recent_working_memories(limit=max(limit, 8))
        if not results:
            return "当前没有仍然有效的短期上下文。"
        sections = self._build_working_memory_sections(results)
        return self._format_working_memory_sections(
            title="当前有效短期上下文：",
            sections=sections,
            max_chars=max_chars,
        )

    def get_recent_working_memories(self, limit: int = 6) -> list[Any]:
        """Return recent working-memory items after TTL and user filtering."""
        working_memory = self.memory_tool.memory_manager.memory_types.get("working")
        if working_memory is None:
            return []
        if hasattr(working_memory, "_expire_old_memories"):
            working_memory._expire_old_memories()

        if hasattr(working_memory, "get_recent"):
            candidates = working_memory.get_recent(limit=max(limit * 3, 12))
        elif hasattr(working_memory, "get_all"):
            candidates = sorted(
                working_memory.get_all(),
                key=lambda memory: memory.timestamp,
                reverse=True,
            )
        else:
            return []

        current_user_id = getattr(self.memory_tool.memory_manager, "user_id", None)
        filtered = [
            memory
            for memory in candidates
            if not memory.metadata.get("forgotten", False)
            and (not current_user_id or memory.user_id == current_user_id)
        ]
        return filtered[:limit]

    def _get_active_working_memories(self, limit: int = 5) -> list[Any]:
        """Return current active working memories with TTL and user filtering applied."""
        working_memory = self.memory_tool.memory_manager.memory_types.get("working")
        if working_memory is None or not hasattr(working_memory, "get_all"):
            return []

        if hasattr(working_memory, "_expire_old_memories"):
            working_memory._expire_old_memories()

        current_user_id = getattr(self.memory_tool.memory_manager, "user_id", None)
        all_memories = working_memory.get_all()
        active_memories = [
            memory
            for memory in all_memories
            if not memory.metadata.get("forgotten", False)
            and (not current_user_id or memory.user_id == current_user_id)
        ]
        if not active_memories:
            return []

        return sorted(
            active_memories,
            key=lambda memory: (memory.importance, memory.timestamp),
            reverse=True,
        )[:limit]

    def _build_working_memory_sections(self, memories: list[Any]) -> dict[str, list[str]]:
        """Group recent working memories into prompt-friendly short-term sections."""
        grouped = {
            "temporary_constraints": [],
            "recent_feedback": [],
            "task_state": [],
            "other": [],
        }

        for memory in memories:
            category = memory.metadata.get("category") or self._infer_working_memory_category(
                memory.content
            )
            item_text = self._truncate_text(
                memory.content.replace("\n", " ").strip(),
                limit=180,
            )
            if not item_text:
                continue
            if category == "temporary_constraint":
                grouped["temporary_constraints"].append(item_text)
            elif category == "recent_feedback":
                grouped["recent_feedback"].append(item_text)
            elif category == "task_state":
                grouped["task_state"].append(item_text)
            else:
                grouped["other"].append(item_text)
        return grouped

    def _format_working_memory_sections(
        self,
        title: str,
        sections: dict[str, list[str]],
        max_chars: int,
    ) -> str:
        """Format grouped short-term state into a compact text block."""
        section_titles = {
            "temporary_constraints": "临时条件",
            "recent_feedback": "近期反馈",
            "task_state": "短期状态",
            "other": "其他短期记录",
        }
        lines = [title]
        current_length = len(title)
        has_content = False

        for key, heading in section_titles.items():
            entries = sections.get(key, [])
            if not entries:
                continue
            has_content = True
            block_lines = [f"- {heading}:"]
            for index, entry in enumerate(entries, start=1):
                block_lines.append(f"  {index}. {entry}")
            block = "\n".join(block_lines)
            if current_length + len(block) + 1 > max_chars:
                remaining = max_chars - current_length
                if remaining > 40:
                    truncated = block[:remaining].rstrip()
                    lines.append(f"{truncated}...")
                break
            lines.append(block)
            current_length += len(block) + 1

        return "\n".join(lines) if has_content else "当前没有仍然有效的短期上下文。"

    def _infer_working_memory_category(self, content: str) -> str:
        """Infer short-term memory category from plain user text."""
        normalized = (content or "").lower()
        temporary_keywords = [
            "今天",
            "今晚",
            "明天",
            "临时",
            "这次",
            "本轮",
            "当前",
            "预算",
            "加班",
            "便利店",
        ]
        feedback_keywords = [
            "更喜欢",
            "不喜欢这种",
            "太长了",
            "太复杂",
            "优化一下",
            "调整一下",
            "改一下",
        ]
        if any(keyword in normalized for keyword in temporary_keywords):
            return "temporary_constraint"
        if any(keyword in normalized for keyword in feedback_keywords):
            return "recent_feedback"
        return "task_state"

    def _truncate_text(self, text: str, limit: int = 160) -> str:
        """Trim long memory lines for prompt/state display."""
        compact = " ".join((text or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3].rstrip() + "..."

    def maybe_distill_turns(self, turns: list[dict[str, str]]) -> list[str]:
        """Batch-distill recent dialogue turns into long-term episodic memory."""
        if not self.distill_enabled:
            self._last_distill_status = "长期记忆提纯已禁用。"
            self._last_distill_source = "disabled"
            return []

        turn_count = len(turns)
        if turn_count < self.distill_every_n_turns:
            self._last_distill_status = (
                f"等待达到提纯阈值：当前 {turn_count} 轮，需要至少 {self.distill_every_n_turns} 轮。"
            )
            self._last_distill_source = "waiting"
            return []
        if turn_count - self._last_distilled_turn_count < self.distill_every_n_turns:
            self._last_distill_status = (
                f"最近已处理到第 {self._last_distilled_turn_count} 轮，等待下一批对话再提纯。"
            )
            self._last_distill_source = "waiting"
            return []

        start_index = self._last_distilled_turn_count
        batch = turns[start_index:turn_count]
        self._last_distill_status = (
            f"正在提纯第 {start_index + 1}-{turn_count} 轮对话中的长期信息。"
        )
        self._last_distill_source = "running"
        distilled_items = self._distill_turn_batch(
            turns=batch,
            start_turn=start_index + 1,
            end_turn=turn_count,
        )
        self._last_distilled_turn_count = turn_count

        if not distilled_items:
            self._last_distill_status = (
                f"已提纯第 {start_index + 1}-{turn_count} 轮，但没有抽取到可沉淀的长期信息。"
            )
            return []

        saved_messages: list[str] = []
        for item in distilled_items:
            save_result = self._save_distilled_memory(
                content=item["content"],
                category=item.get("category", "distilled_preference"),
                importance=item.get("importance", 0.82),
                start_turn=start_index + 1,
                end_turn=turn_count,
            )
            if save_result:
                saved_messages.append(save_result)
        if saved_messages:
            self._last_distill_status = (
                f"已从第 {start_index + 1}-{turn_count} 轮提纯并写入 {len(saved_messages)} 条长期记忆。"
            )
        else:
            self._last_distill_status = (
                f"已提纯第 {start_index + 1}-{turn_count} 轮，但候选内容与现有长期记忆重复，未新增写入。"
            )
        return saved_messages

    def _distill_turn_batch(
        self,
        turns: list[dict[str, str]],
        start_turn: int,
        end_turn: int,
    ) -> list[dict[str, Any]]:
        """Extract stable, non-temporary long-term signals from a turn batch."""
        llm_items = self._distill_turn_batch_with_llm(turns, start_turn, end_turn)
        if llm_items:
            return llm_items
        return self._distill_turn_batch_with_rules(turns)

    def _distill_turn_batch_with_llm(
        self,
        turns: list[dict[str, str]],
        start_turn: int,
        end_turn: int,
    ) -> list[dict[str, Any]]:
        """Use an LLM to distill recent turns into stable episodic memories."""
        llm = self._get_distill_llm()
        if llm is None:
            return []

        serialized_turns = []
        for index, turn in enumerate(turns, start=start_turn):
            serialized_turns.append(
                f"Turn {index}\nUser: {turn['user']}\nAssistant: {turn['assistant']}"
            )
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是一个记忆提纯器。请从最近几轮健康饮食对话里，只提炼值得长期保存到 episodic memory 的稳定信息。"
                    "不要输出任何临时场景、当天安排、一次性预算、一次性餐次、短期加班、仅当前轮有效的条件。"
                    "不要重复结构化画像里已经明确表达的字段：allergies、diet_pattern、goal、dislikes、medical_notes、"
                    "preferred_cuisines、cooking_constraints、notes。"
                    "只保留少量高价值、跨未来多轮仍可能有用的非结构化信息，例如偏好的回答方式、稳定执行习惯、长期反馈。"
                    "返回 JSON 数组，每个元素格式为 "
                    '{"content":"...", "category":"distilled_preference|distilled_feedback|distilled_history", "importance":0.0}. '
                    "如果没有合适内容，返回 []。不要输出任何额外解释。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"当前结构化画像摘要：\n{self.get_current_profile_summary()}\n\n"
                    f"对话范围：Turn {start_turn} - Turn {end_turn}\n\n"
                    + "\n\n".join(serialized_turns)
                ),
            },
        ]
        try:
            response = llm.invoke(prompt)
            self._last_distill_raw_preview = self._truncate_text(response or "", limit=220)
            self._last_distill_source = "llm"
            return self._parse_distilled_items(response)
        except Exception as exc:
            self._last_distill_status = f"LLM 提纯失败，已回退规则提纯：{exc}"
            self._last_distill_source = "rule_fallback"
            return []

    def _get_distill_llm(self) -> HelloAgentsLLM | None:
        """Create a dedicated lightweight distillation LLM when configured."""
        if self._distill_llm is not None:
            return self._distill_llm

        model = os.getenv("DISTILL_MODEL_ID")
        api_key = os.getenv("DISTILL_API_KEY")
        base_url = os.getenv("DISTILL_BASE_URL")
        try:
            if model and api_key and base_url:
                self._distill_llm = HelloAgentsLLM(
                    model=model,
                    api_key=api_key,
                    base_url=base_url,
                    provider="custom",
                    temperature=0.1,
                )
            else:
                self._distill_llm = HelloAgentsLLM(temperature=0.1)
        except Exception:
            self._distill_llm = None
        return self._distill_llm

    def _parse_distilled_items(self, response: str) -> list[dict[str, Any]]:
        """Parse JSON distilled-memory output from the helper LLM."""
        if not response:
            return []
        cleaned = response.strip()
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1 or end < start:
            return []
        payload = cleaned[start : end + 1]
        try:
            items = json.loads(payload)
        except Exception:
            return []

        distilled: list[dict[str, Any]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            content = self._truncate_text(str(item.get("content", "")).strip(), limit=240)
            category = str(item.get("category", "distilled_preference")).strip()
            importance = item.get("importance", 0.82)
            try:
                importance_value = max(0.0, min(1.0, float(importance)))
            except Exception:
                importance_value = 0.82
            if not content or self._looks_temporary(content):
                continue
            distilled.append(
                {
                    "content": content,
                    "category": category or "distilled_preference",
                    "importance": importance_value,
                }
            )
        return distilled

    def _distill_turn_batch_with_rules(
        self,
        turns: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Fallback rule-based distillation when no distillation model is available."""
        patterns = [
            (r"(我更喜欢[^。！？\n]+)", "distilled_preference", 0.8),
            (r"(我通常[^。！？\n]+)", "distilled_history", 0.78),
            (r"(以后[^。！？\n]+)", "distilled_preference", 0.82),
            (r"(请先[^。！？\n]+)", "distilled_feedback", 0.76),
        ]
        distilled: list[dict[str, Any]] = []
        seen: set[str] = set()
        for turn in turns:
            user_text = turn.get("user", "")
            for pattern, category, importance in patterns:
                for match in re.findall(pattern, user_text):
                    content = self._truncate_text(match.strip(), limit=240)
                    normalized = re.sub(r"\s+", "", content)
                    if not content or normalized in seen or self._looks_temporary(content):
                        continue
                    distilled.append(
                        {
                            "content": content,
                            "category": category,
                            "importance": importance,
                        }
                    )
                    seen.add(normalized)
        if distilled:
            self._last_distill_source = "rules"
            self._last_distill_raw_preview = self._truncate_text(
                " | ".join(item["content"] for item in distilled),
                limit=220,
            )
        return distilled

    def _looks_temporary(self, text: str) -> bool:
        """Check whether a candidate memory looks like a transient scene only."""
        normalized = (text or "").lower()
        temporary_markers = [
            "今天",
            "今晚",
            "明天",
            "这次",
            "这顿",
            "本轮",
            "临时",
            "加班",
            "预算",
            "便利店",
        ]
        return any(marker in normalized for marker in temporary_markers)

    def _save_distilled_memory(
        self,
        content: str,
        category: str,
        importance: float,
        start_turn: int,
        end_turn: int,
    ) -> str | None:
        """Persist one distilled long-term memory if it is not a duplicate."""
        normalized_content = re.sub(r"\s+", "", content)
        episodic_memory = self.memory_tool.memory_manager.memory_types.get("episodic")
        if episodic_memory is None or not hasattr(episodic_memory, "doc_store"):
            return None

        user_id = getattr(self.memory_tool.memory_manager, "user_id", None)
        docs = episodic_memory.doc_store.search_memories(
            user_id=user_id,
            memory_type="episodic",
            limit=1000,
        )
        for doc in docs:
            properties = doc.get("properties", {})
            existing_content = re.sub(r"\s+", "", doc.get("content", ""))
            if (
                existing_content == normalized_content
                and properties.get("distilled") is True
                and properties.get("category") == category
            ):
                return None

        memory_id = self.memory_tool.memory_manager.add_memory(
            content=content,
            memory_type="episodic",
            importance=importance,
            metadata={
                "category": category,
                "distilled": True,
                "source_turn_range": f"{start_turn}-{end_turn}",
                "distilled_at": datetime.now().isoformat(),
            },
            auto_classify=False,
        )
        return f"✅ 长期记忆已提纯写入 (ID: {memory_id[:8]}...)"

    def get_distilled_memory_summary(self, limit: int = 5) -> str:
        """Return a compact summary of distilled episodic memories."""
        episodic_memory = self.memory_tool.memory_manager.memory_types.get("episodic")
        if episodic_memory is None or not hasattr(episodic_memory, "doc_store"):
            return "当前没有提纯后的长期记忆。"

        user_id = getattr(self.memory_tool.memory_manager, "user_id", None)
        docs = episodic_memory.doc_store.search_memories(
            user_id=user_id,
            memory_type="episodic",
            limit=1000,
        )
        distilled_docs = [
            doc
            for doc in docs
            if doc.get("properties", {}).get("distilled") is True
        ]
        if not distilled_docs:
            return "当前没有提纯后的长期记忆。"

        distilled_docs.sort(
            key=lambda doc: (doc.get("timestamp", 0), doc.get("created_at", "")),
            reverse=True,
        )
        lines = ["提纯后的长期记忆："]
        for index, doc in enumerate(distilled_docs[:limit], start=1):
            lines.append(f"{index}. {self._truncate_text(doc.get('content', ''), limit=120)}")
        return "\n".join(lines)

    def get_distill_status_summary(self) -> str:
        """Return the latest batch-distillation status for debugging."""
        lines = [f"长期记忆提纯状态：{self._last_distill_status}"]
        lines.append(f"提纯来源：{self._last_distill_source}")
        if self._last_distill_raw_preview:
            lines.append(f"最近提纯输出预览：{self._last_distill_raw_preview}")
        return "\n".join(lines)

    def get_profile_parse_debug_summary(self) -> str:
        """Return latest profile_set parse debug information."""
        return self._last_profile_parse_debug

    def memory_search(self, query: str, limit: int = 5) -> str:
        """Retrieve relevant long-term user memory from episodic memory."""
        return self.memory_tool.run(
            {
                "action": "search",
                "query": query,
                "memory_type": "episodic",
                "limit": limit,
            }
        )

    def memory_remember(self, content: str, importance: float = 0.9) -> str:
        """Store non-structured long-term preference or history."""
        return self.memory_tool.run(
            {
                "action": "add",
                "content": content,
                "memory_type": "episodic",
                "importance": importance,
            }
        )

    def memory_digest(self, limit: int = 10) -> str:
        """Return a high-level summary of the memory system."""
        return self.memory_tool.run({"action": "summary", "limit": limit})

    def kb_search(self, query: str, limit: int = 5) -> str:
        """Run retrieval-only search against the nutrition knowledge base."""
        return self.rag_tool.run(
            {
                "action": "search",
                "query": query,
                "namespace": self.rag_namespace,
                "enable_advanced_search": True,
                "enable_mqe": True,
                "enable_hyde": False,
                "include_citations": True,
                "limit": limit,
            }
        )

    def kb_answer(self, question: str, limit: int = 5) -> str:
        """Answer a question using the nutrition knowledge base."""
        return self.rag_tool.run(
            {
                "action": "ask",
                "question": question,
                "namespace": self.rag_namespace,
                "enable_advanced_search": True,
                "enable_mqe": True,
                "enable_hyde": True,
                "include_citations": True,
                "limit": limit,
            }
        )

    def kb_status(self) -> str:
        """Return the current namespace stats for the knowledge base."""
        return self.rag_tool.run(
            {
                "action": "stats",
                "namespace": self.rag_namespace,
            }
        )

    def seed_default_knowledge(self) -> list[str]:
        """Bootstrap the nutrition knowledge base from local markdown files."""
        results: list[str] = []
        for file_path in sorted(self.knowledgebase_dir.glob("*.md")):
            result = self.rag_tool.run(
                {
                    "action": "add_document",
                    "file_path": str(file_path),
                    "document_id": file_path.stem,
                    "namespace": self.rag_namespace,
                }
            )
            results.append(result)
        return results

    def get_state_summary(self) -> str:
        """Return a compact summary of memory and knowledge state."""
        memory_summary = self.memory_digest(limit=10)
        rag_summary = self.kb_status()
        working_summary = self.get_working_context_summary(limit=8, max_chars=800)
        distilled_summary = self.get_distilled_memory_summary(limit=5)
        return (
            f"Memory summary:\n{memory_summary}\n\n"
            f"Working summary:\n{working_summary}\n\n"
            f"Distilled summary:\n{distilled_summary}\n\n"
            f"RAG summary:\n{rag_summary}"
        )

    def list_knowledgebase_files(self) -> list[dict[str, Any]]:
        """Return available knowledgebase source documents."""
        files: list[dict[str, Any]] = []
        for file_path in sorted(self.knowledgebase_dir.glob("*.md")):
            files.append(
                {
                    "name": file_path.name,
                    "stem": file_path.stem,
                    "path": str(file_path),
                    "size_bytes": file_path.stat().st_size,
                }
            )
        return files

    def read_knowledgebase_file(self, name: str) -> dict[str, Any] | None:
        """Read a single knowledgebase markdown file by name."""
        file_name = Path(name).name
        file_path = self.knowledgebase_dir / file_name
        if not file_path.exists() or not file_path.is_file():
            return None
        return {
            "name": file_path.name,
            "stem": file_path.stem,
            "content": file_path.read_text(encoding="utf-8"),
        }

    def get_state_dict(self) -> dict[str, Any]:
        """Return structured state for API and frontend consumers."""
        memory_summary = self.memory_digest(limit=10)
        rag_summary = self.kb_status()
        current_profile = self.get_current_profile()
        return {
            "user_id": getattr(self.memory_tool.memory_manager, "user_id", "unknown"),
            "rag_namespace": self.rag_namespace,
            "knowledgebase_dir": str(self.knowledgebase_dir),
            "memory_summary": memory_summary,
            "rag_summary": rag_summary,
            "working_memory_summary": self.get_working_context_summary(limit=8, max_chars=1200),
            "distilled_memory_summary": self.get_distilled_memory_summary(limit=5),
            "distill_status_summary": self.get_distill_status_summary(),
            "profile_parse_debug_summary": self.get_profile_parse_debug_summary(),
            "current_profile": current_profile.to_dict(),
            "current_profile_summary": current_profile.to_summary_text(),
            "knowledgebase_files": self.list_knowledgebase_files(),
            "distill_every_n_turns": self.distill_every_n_turns,
            "memory_tool_session_id": self.memory_tool.current_session_id or "未开始",
            "memory_tool_conversation_count": self.memory_tool.conversation_count,
        }

    def clear_user_memories(self) -> dict[str, str]:
        """Clear only the current user's memories for debugging."""
        user_id = getattr(self.memory_tool.memory_manager, "user_id", "unknown")
        self.memory_tool.memory_manager.clear_user_memories(user_id=user_id)
        self.memory_tool.current_session_id = None
        self.memory_tool.conversation_count = 0
        self._last_distilled_turn_count = 0
        self._last_distill_status = "尚未触发长期记忆提纯。"
        self._last_distill_source = "none"
        self._last_distill_raw_preview = ""
        self._last_profile_parse_debug = "尚未解析 profile_set 输入。"
        return {
            "message": f"已清空用户 {user_id} 的记忆",
            "user_id": user_id,
        }

    def get_current_profile(self) -> WellnessProfile:
        """Return the latest persisted current profile for the active user."""
        profile_doc = self._find_current_profile_memory()
        if not profile_doc:
            return WellnessProfile()
        return WellnessProfile.from_memory_text(profile_doc["content"])

    def get_current_profile_summary(self) -> str:
        """Return the active profile as readable text."""
        profile = self.get_current_profile()
        if profile.is_empty():
            return "当前用户画像为空。"
        return profile.to_summary_text()

    def upsert_current_profile(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Merge partial field updates into the current profile memory."""
        current_doc = self._find_current_profile_memory()
        current_profile = (
            WellnessProfile.from_memory_text(current_doc["content"])
            if current_doc
            else WellnessProfile()
        )
        updated_profile = current_profile.merged(updates)
        saved = self._save_current_profile(
            updated_profile,
            existing_doc=current_doc,
            importance=1.0,
        )
        return {
            "memory_id": saved["memory_id"],
            "profile": updated_profile,
            "updated_fields": sorted(
                field_name
                for field_name in updates
                if field_name in WellnessProfile.SUPPORTED_FIELDS
            ),
        }

    def remove_current_profile_fields(self, fields: list[str]) -> dict[str, Any]:
        """Clear specific fields from the current profile memory."""
        normalized_fields = [
            field_name
            for field_name in fields
            if field_name in WellnessProfile.SUPPORTED_FIELDS
        ]
        current_doc = self._find_current_profile_memory()
        current_profile = (
            WellnessProfile.from_memory_text(current_doc["content"])
            if current_doc
            else WellnessProfile()
        )
        updated_profile = current_profile.without_fields(normalized_fields)

        if updated_profile.is_empty():
            if current_doc:
                self.memory_tool.memory_manager.remove_memory(current_doc["memory_id"])
            return {
                "memory_id": current_doc["memory_id"] if current_doc else "",
                "profile": updated_profile,
                "removed_fields": normalized_fields,
                "deleted_profile": True,
            }

        saved = self._save_current_profile(
            updated_profile,
            existing_doc=current_doc,
            importance=1.0,
        )
        return {
            "memory_id": saved["memory_id"],
            "profile": updated_profile,
            "removed_fields": normalized_fields,
            "deleted_profile": False,
        }

    def _find_current_profile_memory(self) -> dict[str, Any] | None:
        """Locate the most recent structured current-profile memory."""
        episodic_memory = self.memory_tool.memory_manager.memory_types.get("episodic")
        if episodic_memory is None or not hasattr(episodic_memory, "doc_store"):
            return None

        user_id = getattr(self.memory_tool.memory_manager, "user_id", None)
        docs = episodic_memory.doc_store.search_memories(
            user_id=user_id,
            memory_type="episodic",
            limit=1000,
        )
        profile_docs = [
            doc
            for doc in docs
            if doc.get("properties", {}).get("category") == "current_profile"
            or doc.get("content", "").startswith(WellnessProfile.MEMORY_HEADER)
        ]
        if not profile_docs:
            return None
        profile_docs.sort(
            key=lambda doc: (doc.get("timestamp", 0), doc.get("created_at", "")),
            reverse=True,
        )
        return profile_docs[0]

    def _save_current_profile(
        self,
        profile: WellnessProfile,
        existing_doc: dict[str, Any] | None = None,
        importance: float = 1.0,
    ) -> dict[str, Any]:
        """Persist the current profile, updating the existing record when possible."""
        metadata = {
            "category": "current_profile",
            "profile_version": "current",
            "updated_at": datetime.now().isoformat(),
        }
        memory_manager = self.memory_tool.memory_manager

        if existing_doc:
            merged_properties = {**existing_doc.get("properties", {}), **metadata}
            memory_manager.update_memory(
                memory_id=existing_doc["memory_id"],
                content=profile.to_memory_text(),
                importance=importance,
                metadata=merged_properties,
            )
            return {
                "memory_id": existing_doc["memory_id"],
                "profile": profile,
            }

        memory_id = memory_manager.add_memory(
            content=profile.to_memory_text(),
            memory_type="episodic",
            importance=importance,
            metadata=metadata,
            auto_classify=False,
        )
        return {
            "memory_id": memory_id,
            "profile": profile,
        }
