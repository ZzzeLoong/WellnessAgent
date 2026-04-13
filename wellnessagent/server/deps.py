"""Shared dependencies and agent lifecycle helpers for FastAPI."""

from pathlib import Path

from ..agent import WellnessPlanningAgent
from ..schemas import WellnessProfile


_AGENTS: dict[str, WellnessPlanningAgent] = {}
_BOOTSTRAPPED_USERS: set[str] = set()


def get_agent(user_id: str) -> WellnessPlanningAgent:
    """Return a cached agent per user."""
    if user_id not in _AGENTS:
        _AGENTS[user_id] = WellnessPlanningAgent(user_id=user_id)
    return _AGENTS[user_id]


def ensure_knowledgebase_seeded(user_id: str) -> None:
    """Seed the default knowledgebase once per user per process."""
    if user_id in _BOOTSTRAPPED_USERS:
        return
    agent = get_agent(user_id)
    agent.seed_knowledge_base()
    _BOOTSTRAPPED_USERS.add(user_id)


def apply_profile(user_id: str, profile: WellnessProfile) -> str:
    """Persist a profile and ensure the user knowledgebase is ready."""
    ensure_knowledgebase_seeded(user_id)
    agent = get_agent(user_id)
    return agent.onboard_user(profile)


def list_knowledgebase_files(user_id: str) -> list[dict]:
    """List raw knowledgebase documents for a user."""
    ensure_knowledgebase_seeded(user_id)
    return get_agent(user_id).list_knowledgebase_files()


def read_knowledgebase_file(user_id: str, name: str) -> dict | None:
    """Read a raw knowledgebase document for a user."""
    ensure_knowledgebase_seeded(user_id)
    return get_agent(user_id).read_knowledgebase_file(name)


def clear_user_memories(user_id: str) -> dict[str, str]:
    """Clear only the specified user's memories."""
    agent = get_agent(user_id)
    return agent.clear_user_memories()


def get_frontend_dist() -> Path:
    """Return the built frontend directory."""
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"
