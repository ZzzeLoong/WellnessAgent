"""Shared dependencies and agent lifecycle helpers for FastAPI.

Agent 缓存改为 **LRU**（D3）：超过 ``WELLNESS_MAX_AGENTS`` 时淘汰最久未用者，
淘汰前 ``cleanup()``（内部 ``_persist_session`` 落盘 + 清 working memory），数据不丢；
用户再来时 ``get_agent`` 新建实例并从 ``SessionStore`` 恢复最近会话（R17 重建）。

seed 幂等改为 **落盘指纹**（D8）：把知识库 raw 目录的文件指纹写入
``logs/kb_seed/<user_id>.seeded``；指纹一致则跳过 seed，变化则重 seed，
与 D3 淘汰解耦（重启/淘汰后不会重复 seed）。
"""

import hashlib
import json
import os
from collections import OrderedDict
from pathlib import Path

from ..agent import WellnessPlanningAgent
from ..schemas import WellnessProfile


_MAX_AGENTS = int(os.getenv("WELLNESS_MAX_AGENTS", "50"))
_AGENTS: "OrderedDict[str, WellnessPlanningAgent]" = OrderedDict()

_KB_RAW_DIR = Path(__file__).resolve().parents[1] / "knowledgebase" / "raw"
_KB_SEED_DIR = Path(os.getenv("WELLNESS_KB_SEED_DIR", "logs/kb_seed"))


def _evict_if_needed() -> None:
    """Evict least-recently-used agents beyond the configured cap."""
    while len(_AGENTS) > _MAX_AGENTS:
        _, agent = _AGENTS.popitem(last=False)
        try:
            agent.cleanup()
        except Exception:
            pass


def get_agent(user_id: str) -> WellnessPlanningAgent:
    """Return a cached agent per user (LRU); rebuild session on cache miss."""
    if user_id in _AGENTS:
        _AGENTS.move_to_end(user_id)
        return _AGENTS[user_id]

    agent = WellnessPlanningAgent(user_id=user_id)
    # 缓存未命中：尝试恢复最近一次会话（重启/淘汰后续接，R17）。
    try:
        latest = agent.session_store.latest_session_id(user_id)
        if latest:
            agent.load_session(latest)
    except Exception:
        pass

    _AGENTS[user_id] = agent
    _AGENTS.move_to_end(user_id)
    _evict_if_needed()
    return agent


# ---------------------------------------------------------------- seed idempotency
def _knowledgebase_fingerprint() -> str:
    """Compute a stable fingerprint over the raw knowledgebase files."""
    if not _KB_RAW_DIR.exists():
        return "empty"
    digest = hashlib.sha256()
    for path in sorted(_KB_RAW_DIR.glob("*.md")):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(str(stat.st_size).encode("utf-8"))
        digest.update(str(int(stat.st_mtime)).encode("utf-8"))
    return digest.hexdigest()


def _seed_marker_path(user_id: str) -> Path:
    return _KB_SEED_DIR / f"{user_id}.seeded"


def _is_seeded(user_id: str, fingerprint: str) -> bool:
    """Return True if the user has already been seeded with this fingerprint."""
    path = _seed_marker_path(user_id)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("fingerprint") == fingerprint


def _mark_seeded(user_id: str, fingerprint: str) -> None:
    """Persist the seed marker with the current knowledgebase fingerprint."""
    _KB_SEED_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"user_id": user_id, "fingerprint": fingerprint}
    path = _seed_marker_path(user_id)
    tmp = path.with_suffix(".seeded.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def ensure_knowledgebase_seeded(user_id: str) -> None:
    """Seed the default knowledgebase once per user, keyed by content fingerprint."""
    fingerprint = _knowledgebase_fingerprint()
    if _is_seeded(user_id, fingerprint):
        return
    agent = get_agent(user_id)
    agent.seed_knowledge_base()
    _mark_seeded(user_id, fingerprint)


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


# ---------------------------------------------------------------- session mgmt
def list_sessions(user_id: str) -> list[dict]:
    """List persisted conversation sessions for a user (R17)."""
    return get_agent(user_id).session_store.list_sessions(user_id)


def new_session(user_id: str) -> str:
    """Start a fresh conversation session and return its id."""
    return get_agent(user_id).new_session()


# ---------------------------------------------------------------- trace access
_TRACE_DIR = Path(os.getenv("WELLNESS_TRACE_DIR", "logs/traces"))


def list_traces(user_id: str) -> list[dict]:
    """List available trace files for a user (R5)."""
    user_dir = _TRACE_DIR / user_id
    if not user_dir.exists():
        return []
    traces: list[dict] = []
    for path in sorted(user_dir.glob("trace-*.jsonl"), reverse=True):
        session_id = path.stem[len("trace-"):]
        try:
            stat = path.stat()
        except OSError:
            continue
        traces.append(
            {
                "trace_id": session_id,
                "session_id": session_id,
                "size_bytes": stat.st_size,
                "updated_at": stat.st_mtime,
            }
        )
    return traces


def read_metrics(user_id: str | None = None, since: str | None = None) -> dict:
    """Aggregate metrics from trace JSONL (R8)。user_id 为空聚合所有用户。"""
    from ...observability.metrics import aggregate_metrics

    normalized_user = user_id or None
    return aggregate_metrics(user_id=normalized_user, since=since or None)


def read_trace(user_id: str, session_id: str) -> dict | None:
    """Read one trace's JSONL events for replay (R5)."""
    path = _TRACE_DIR / user_id / f"trace-{session_id}.jsonl"
    if not path.exists():
        return None
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return {"trace_id": session_id, "session_id": session_id, "events": events}


def get_frontend_dist() -> Path:
    """Return the built frontend directory."""
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"
