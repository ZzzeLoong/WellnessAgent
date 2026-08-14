"""Shared dependencies and agent lifecycle helpers for FastAPI.

Agent 缓存改为 **LRU**（D3）：超过 ``WELLNESS_MAX_AGENTS`` 时淘汰最久未用者，
淘汰前 ``cleanup()``（内部 ``_persist_session`` 落盘 + 清 working memory），数据不丢；
用户再来时 ``get_agent`` 新建实例并从 ``SessionStore`` 恢复最近会话（R17 重建）。

seed 幂等改为 **落盘指纹**（D8）：把知识库 raw 目录的文件指纹写入
``logs/kb_seed/<user_id>.seeded``；指纹一致则跳过 seed，变化则重 seed，
与 D3 淘汰解耦（重启/淘汰后不会重复 seed）。

线程安全（Phase 3.0-3）：``OrderedDict`` 的 ``popitem`` / ``move_to_end`` /
``in`` + 赋值序列在多线程下均非原子，而三期 R10 的推送 tick 线程会并发访问
同一份 ``_AGENTS``（uvicorn 的同步路径本身也跑在线程池里）。不加锁会出现：

1. 两个线程同时 miss → 同一 user 被构造两个实例，其中一个默默丢弃（会话/记忆分叉）；
2. 淘汰与 ``move_to_end`` 交错 → ``KeyError``。

因此所有读写 ``_AGENTS`` 的入口统一在 ``_AGENTS_LOCK``（``RLock``）下执行。
用 ``RLock`` 而非 ``Lock``：``new_session`` 等包装函数会嵌套调用 ``get_agent``，
需要可重入。代价是不同用户的首次构造被串行化 —— 对本项目的并发量级
（单进程 + LRU 50）完全可接受，且正好避免了多个用户实例同时初始化
嵌入模型/向量库造成的资源尖峰。
"""

import hashlib
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path

from ...core.media_store import MediaStore
from ..agent import WellnessPlanningAgent
from ..schemas import WellnessProfile


_MAX_AGENTS = int(os.getenv("WELLNESS_MAX_AGENTS", "50"))
_AGENTS: "OrderedDict[str, WellnessPlanningAgent]" = OrderedDict()
# P3.0-3：保护 _AGENTS 的可重入锁（原因详见模块 docstring）。
_AGENTS_LOCK = threading.RLock()

_KB_RAW_DIR = Path(__file__).resolve().parents[1] / "knowledgebase" / "raw"
_KB_SEED_DIR = Path(os.getenv("WELLNESS_KB_SEED_DIR", "logs/kb_seed"))


def _evict_if_needed() -> None:
    """Evict least-recently-used agents beyond the configured cap.

    调用方必须已持有 ``_AGENTS_LOCK``（当前唯一调用方 ``get_agent`` 满足）。
    """
    while len(_AGENTS) > _MAX_AGENTS:
        _, agent = _AGENTS.popitem(last=False)
        try:
            agent.cleanup()
        except Exception:
            pass


def get_agent(user_id: str) -> WellnessPlanningAgent:
    """Return a cached agent per user (LRU); rebuild session on cache miss.

    线程安全（P3.0-3）：命中判定、实例构造、写入缓存与淘汰全部在同一临界区内，
    保证并发下同一 ``user_id`` 只会拿到**同一个**实例。
    """
    with _AGENTS_LOCK:
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


def reset_agents() -> int:
    """Drop every cached agent (cleanup first) and return how many were dropped.

    供测试隔离与后续 R14 的 variant 切换使用（不清缓存会复用旧配置实例，
    使 A/B 结论失真）。
    """
    with _AGENTS_LOCK:
        count = len(_AGENTS)
        while _AGENTS:
            _, agent = _AGENTS.popitem(last=False)
            try:
                agent.cleanup()
            except Exception:
                pass
        return count


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
    """Start a fresh conversation session and return its id.

    「取实例 + 开新会话」在同一临界区内完成，避免与并发 LRU 淘汰交错（P3.0-3）：
    否则实例可能在 ``new_session()`` 前被淘汰并 ``cleanup()``，返回的会话 id
    落在一个已被丢弃的实例上。
    """
    with _AGENTS_LOCK:
        return get_agent(user_id).new_session()


# ---------------------------------------------------------------- media (R9)
_MEDIA_STORE: MediaStore | None = None
_MEDIA_LOCK = threading.RLock()


def get_media_store() -> MediaStore:
    """Return the process-wide :class:`MediaStore` (lazily constructed).

    延迟到首次使用才构造：读环境变量的时机与 ``load_dotenv`` 解耦，
    也让不开多模态的部署完全不碰媒体目录。
    """
    global _MEDIA_STORE
    with _MEDIA_LOCK:
        if _MEDIA_STORE is None:
            _MEDIA_STORE = MediaStore()
        return _MEDIA_STORE


def reset_media_store() -> None:
    """Drop the cached MediaStore (供单测切换 ``WELLNESS_MEDIA_DIR``)。"""
    global _MEDIA_STORE
    with _MEDIA_LOCK:
        _MEDIA_STORE = None


def vision_enabled() -> bool:
    """R9 多模态总开关（``WELLNESS_VLM_ENABLED``，默认关闭）。

    关闭时上传接口返回 501、``_get_vision_llm()`` 返回 None，
    行为与二期完全一致（方案 §10.3）。
    """
    return os.getenv("WELLNESS_VLM_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# ---------------------------------------------------------------- advice (R11)
def list_advice(user_id: str, status: str = "", limit: int = 50) -> dict:
    """列出建议记录及各状态计数（``GET /api/advice/list``）。"""
    store = get_agent(user_id).advice_store
    records = store.list(user_id, status=status or None, limit=limit)
    return {
        "items": [r.to_dict() for r in records],
        "counts_by_status": store.counts_by_status(user_id),
    }


def submit_advice_feedback(
    user_id: str, advice_id: str, status: str, note: str = ""
) -> dict:
    """前端手动打标（``POST /api/advice/feedback``，便于 demo）。

    复用 agent 的 ``_advice_feedback`` —— 与模型调用工具**完全同一条路径**，
    因此状态更新与 episodic 记忆写入两个副作用都不会漏。
    """
    agent = get_agent(user_id)
    message = agent._advice_feedback(advice_id=advice_id, status=status, note=note)
    ok = message.startswith("✅")
    return {"ok": ok, "message": message, "advice_id": advice_id}


# ---------------------------------------------------------------- push (R10)
def get_push_plan(user_id: str) -> dict:
    """读推送计划（``GET /api/push/plan``）。"""
    from ..scheduler import get_scheduler

    return get_scheduler().plan_store.load(user_id).to_dict()


def update_push_plan(user_id: str, **changes) -> dict:
    """更新推送计划（``POST /api/push/plan``）。

    计划变更后按需拉起/停止 tick 线程：用户在 UI 打开推送时，不必重启进程。
    """
    from ..scheduler import get_scheduler

    scheduler = get_scheduler()
    plan = scheduler.plan_store.update(user_id, **changes)
    if plan.enabled:
        scheduler.start()  # 幂等：总开关关闭或已在跑时直接返回 False
    return plan.to_dict()


def run_push_now(user_id: str) -> dict:
    """立即生成并投递一条推送（``POST /api/push/run-now``，demo 必需）。

    **不受 ``WELLNESS_PUSH_ENABLED`` 与 ``plan.enabled`` 限制** —— 否则演示时
    得先开全局调度再等到点（方案 §4.2）。
    """
    from ..scheduler import get_scheduler

    return get_scheduler().run_now(user_id, mark=False).to_dict()


def list_push_inbox(user_id: str, unread_only: bool = False, limit: int = 50) -> dict:
    """读收件箱（``GET /api/push/inbox``）。"""
    from ..scheduler import get_scheduler

    inbox = get_scheduler().inbox
    return {
        "items": [m.to_dict() for m in inbox.list(user_id, unread_only, limit)],
        "unread_count": inbox.unread_count(user_id),
    }


def mark_push_read(user_id: str, push_id: str = "", all_read: bool = False) -> dict:
    """标记已读（``POST /api/push/read``）。``all_read`` 为真时全部标记。"""
    from ..scheduler import get_scheduler

    inbox = get_scheduler().inbox
    if all_read:
        count = inbox.mark_all_read(user_id)
        return {"ok": True, "marked": count, "unread_count": inbox.unread_count(user_id)}
    ok = inbox.mark_read(user_id, push_id)
    return {"ok": ok, "marked": 1 if ok else 0, "unread_count": inbox.unread_count(user_id)}


def start_push_scheduler() -> bool:
    """FastAPI 启动钩子：``WELLNESS_PUSH_ENABLED=true`` 时拉起 tick 线程。"""
    from ..scheduler import get_scheduler

    try:
        return get_scheduler().start()
    except Exception:
        return False


def stop_push_scheduler() -> None:
    """FastAPI 关闭钩子：停掉 tick 线程。"""
    from ..scheduler import get_scheduler

    try:
        get_scheduler().stop()
    except Exception:
        pass


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
