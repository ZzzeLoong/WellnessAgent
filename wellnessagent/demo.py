"""Runnable demo for the first wellness agent prototype."""

from __future__ import annotations

import contextlib
import importlib
import io
import logging
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> None:
    """Load .env from the repository root with a small fallback parser."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return

    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return
    except Exception:
        pass

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _import_business_package():
    """Import the business package in both script and module modes."""
    if __package__:
        from . import WellnessPlanningAgent, WellnessProfile

        return WellnessPlanningAgent, WellnessProfile

    repo_parent = REPO_ROOT.parent
    if str(repo_parent) not in sys.path:
        sys.path.insert(0, str(repo_parent))

    package_name = REPO_ROOT.name
    module = importlib.import_module(f"{package_name}.wellnessagent")
    return module.WellnessPlanningAgent, module.WellnessProfile


def _validate_env() -> None:
    """Validate the minimum environment required for the demo."""
    required_keys = [
        "LLM_MODEL_ID",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "QDRANT_URL",
        "EMBED_MODEL_TYPE",
    ]
    missing = [key for key in required_keys if not os.getenv(key)]
    if missing:
        missing_str = ", ".join(missing)
        raise RuntimeError(
            f"缺少必要环境变量: {missing_str}。请先填写仓库根目录下的 .env 文件。"
        )

    embed_type = os.getenv("EMBED_MODEL_TYPE", "").strip().lower()
    if embed_type == "dashscope" and not os.getenv("EMBED_API_KEY"):
        raise RuntimeError(
            "当 EMBED_MODEL_TYPE=dashscope 时，必须提供 EMBED_API_KEY。"
        )


def _print_trace(turn_result: dict[str, object]) -> None:
    """Pretty-print the ReAct trace for one turn."""
    print(f"终止原因: {turn_result['terminated_reason']}")
    for step in turn_result.get("steps", []):
        step_index = step.get("step_index")
        thought = step.get("thought") or "(无)"
        action_text = step.get("action_text") or "(无)"
        observation = _summarize_observation(
            action_text,
            (step.get("observation") or "(无)").strip(),
        )
        print(f"  Step {step_index}")
        print(f"    Thought: {thought}")
        print(f"    Action: {action_text}")
        print(f"    Observation: {observation}")
        if step.get("action_debug"):
            print(f"    ActionDebug: {step['action_debug']}")


def _summarize_observation(action_text: str, observation: str) -> str:
    """Compress verbose tool observations into concise status text."""
    if not observation or observation == "(无)":
        return "(无)"

    action_name = action_text.split("[", 1)[0].strip()
    if action_name == "kb_search":
        return "RAG 检索已完成。"
    if action_name == "kb_answer":
        return "RAG 问答已完成。"
    if action_name == "kb_status":
        return "知识库状态已获取。"

    lines = [line.strip() for line in observation.splitlines() if line.strip()]
    if not lines:
        return "(无)"

    if action_name.startswith("profile_"):
        return " / ".join(lines[:3])
    if action_name.startswith("session_"):
        return " / ".join(lines[:2])
    if action_name.startswith("memory_"):
        return " / ".join(lines[:2])

    compact = " / ".join(lines[:2])
    return compact if len(compact) <= 160 else f"{compact[:160]}..."


def _print_state_snapshot(agent) -> None:
    """Print a compact state snapshot after each turn."""
    state = agent.get_state_dict()
    print("\n  [当前画像]")
    print(state["current_profile_summary"])
    print("\n  [最近对话窗口]")
    print(state.get("recent_dialogue_window", "当前没有历史对话。"))
    print("\n  [短期状态]")
    print(state.get("working_memory_summary", "当前没有仍然有效的短期上下文。"))
    print("\n  [提纯后的长期记忆]")
    print(state.get("distilled_memory_summary", "当前没有提纯后的长期记忆。"))
    print("\n  [长期记忆提纯状态]")
    print(state.get("distill_status_summary", "长期记忆提纯状态未知。"))
    print("\n  [profile_set 调试]")
    print(state.get("profile_parse_debug_summary", "尚无 profile_set 调试信息。"))
    print("\n  [记忆概览]")
    memory_lines = [line.strip() for line in state["memory_summary"].splitlines() if line.strip()]
    print("\n".join(memory_lines[:4]))
    print("\n  [会话调试]")
    print(f"session_id: {state.get('memory_tool_session_id', '未开始')}")
    print(f"completed_turn_count: {state.get('completed_turn_count', 0)}")
    print(f"memory_tool_conversation_count: {state.get('memory_tool_conversation_count', 0)}")
    print("\n  [知识库概览]")
    rag_lines = [line.strip() for line in state["rag_summary"].splitlines() if line.strip()]
    print("\n".join(rag_lines[:5]))


def _run_quietly(func, *args, **kwargs):
    """Run a callable while suppressing noisy stdout/stderr."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        return func(*args, **kwargs)


def _configure_demo_logging() -> None:
    """Reduce noisy third-party logging for demo readability."""
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("WellnessAgent.memory.storage.qdrant_store").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    """Run a multi-turn demo that exercises current agent capabilities."""
    _load_env()
    _validate_env()
    _configure_demo_logging()
    WellnessPlanningAgent, WellnessProfile = _import_business_package()

    user_id = os.getenv("WELLNESS_DEMO_USER_ID", "wellness_demo_user")
    reset_demo_data = os.getenv("WELLNESS_RESET_DEMO_DATA", "true").lower() == "true"

    print("=== Wellness Agent Demo ===")
    print(f"user_id: {user_id}")

    agent = WellnessPlanningAgent(user_id=user_id)

    try:
        if reset_demo_data:
            print("\n[1/3] 清理 demo 数据")
            print(agent.clear_user_memories()["message"])
            rag_reset = _run_quietly(
                agent.rag_tool.run,
                {
                    "action": "clear",
                    "confirm": True,
                    "namespace": agent.rag_tool.rag_namespace,
                }
            )
            print(rag_reset)

        print("\n[2/3] 导入知识库")
        kb_results = _run_quietly(agent.seed_knowledge_base)
        print(f"知识库导入完成，共处理 {len(kb_results)} 个文件。")

        print("\n[3/3] 多轮对话测试")
        print("本轮 demo 从空画像开始，通过真实对话观察 agent 是否会主动维护画像、短期上下文和知识检索。\n")

        conversations = [
            (
                "建立长期画像并给出首版计划",
                "我对花生和虾过敏，平时基本是素食主义者，平常主要吃素，鸡蛋和奶制品可以接受，目标是减脂。"
                "希望早餐简单、午餐可以带饭，工作日做饭最好不要超过20分钟。"
                "请先给我安排1天饮食建议。",
            ),
            (
                "补充长期偏好",
                "再补充一点，我不喜欢芹菜，比较偏好中式家常口味。"
                "另外我更喜欢你先给结论，再补充解释。"
                "你可以据此把刚才的1天计划再优化一下吗？",
            ),
            (
                "注入临时会话条件",
                "今天晚上我要加班，只能在便利店解决晚餐，预算30元以内。"
                "这只是今天的临时情况，请你单独给我一个今晚可执行的选择。",
            ),
            (
                "修正长期约束",
                "之前说得不准确，我其实不是素食主义者，现在改成均衡饮食。"
                "另外我已经没有特别讨厌的食物了，把不喜欢芹菜这条去掉。",
            ),
            (
                "让 agent 回顾长期与短期信息",
                "请总结一下你现在记住了我的哪些长期信息，以及今天这一轮对话里的临时情况。"
                "然后基于这些信息，再给我一个明天的早餐和午餐建议。",
            ),
        ]

        for turn_index, (title, user_message) in enumerate(conversations, start=1):
            print(f"\n--- 对话 {turn_index}: {title} ---")
            print(f"用户: {user_message}")
            turn_result = _run_quietly(agent.chat_with_trace, user_message)
            print(f"助手: {turn_result['answer']}")
            _print_trace(turn_result)
            _print_state_snapshot(agent)

    finally:
        agent.cleanup()


if __name__ == "__main__":
    main()
