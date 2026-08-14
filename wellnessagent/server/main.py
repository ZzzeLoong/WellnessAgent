"""FastAPI application for the wellness agent web UI."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from ...core.media_store import (
    MediaStoreError,
    MediaTooLargeError,
    UnsupportedMediaTypeError,
)
from ..schemas import WellnessProfile
from .deps import (
    apply_profile,
    clear_user_memories,
    ensure_knowledgebase_seeded,
    get_agent,
    get_frontend_dist,
    get_media_store,
    get_push_plan,
    list_advice,
    list_knowledgebase_files,
    list_push_inbox,
    list_sessions,
    list_traces,
    mark_push_read,
    new_session,
    read_knowledgebase_file,
    read_metrics,
    read_trace,
    run_push_now,
    start_push_scheduler,
    stop_push_scheduler,
    submit_advice_feedback,
    update_push_plan,
    vision_enabled,
)
from .schemas import (
    AdviceFeedbackRequest,
    ChatRequest,
    NewSessionRequest,
    ProfileRequest,
    PushPlanRequest,
    PushReadRequest,
    UserScopedRequest,
)


load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """R10 调度器生命周期（方案 §4.1）。

    用 ``lifespan`` 而非方案里写的 ``@app.on_event`` —— 后者在 FastAPI 0.9x+
    已废弃。行为等价：启动时按 ``WELLNESS_PUSH_ENABLED`` 拉起 tick 线程
    （默认 ``false`` → 不启动任何线程），关闭时停掉。
    """
    start_push_scheduler()
    try:
        yield
    finally:
        stop_push_scheduler()


app = FastAPI(title="WellnessAgent API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/api/chat")
def chat(payload: ChatRequest) -> dict:
    """Run one traced chat turn and return the updated state."""
    ensure_knowledgebase_seeded(payload.user_id)
    agent = get_agent(payload.user_id)
    agent.select_session(payload.session_id)
    confirmation = payload.confirmation.model_dump() if payload.confirmation else None
    return agent.chat_with_trace(
        payload.message,
        confirmation=confirmation,
        attachments=[a.model_dump() for a in payload.attachments],
    )


@app.post("/api/chat/stream")
def chat_stream(payload: ChatRequest) -> StreamingResponse:
    """Run one chat turn and stream SSE events (R2)."""
    ensure_knowledgebase_seeded(payload.user_id)
    agent = get_agent(payload.user_id)
    agent.select_session(payload.session_id)
    confirmation = payload.confirmation.model_dump() if payload.confirmation else None
    attachments = [a.model_dump() for a in payload.attachments]

    def event_source():
        for event in agent.chat_stream(
            payload.message, confirmation=confirmation, attachments=attachments
        ):
            yield event.to_sse()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/state")
def state(user_id: str = "web_user") -> dict:
    """Return the current structured state for a user."""
    ensure_knowledgebase_seeded(user_id)
    return get_agent(user_id).get_state_dict()


@app.get("/api/session/list")
def session_list(user_id: str = "web_user") -> dict:
    """List persisted conversation sessions for a user (R17)."""
    return {"sessions": list_sessions(user_id)}


@app.post("/api/session/new")
def session_new(payload: NewSessionRequest) -> dict:
    """Start a fresh conversation session (clears history + working memory)."""
    ensure_knowledgebase_seeded(payload.user_id)
    session_id = new_session(payload.user_id)
    return {"session_id": session_id}


@app.get("/api/trace")
def trace(user_id: str = "web_user", session_id: str = "") -> dict:
    """Read one trace's events for replay (R5)."""
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    result = read_trace(user_id, session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return result


@app.get("/api/trace/list")
def trace_list(user_id: str = "web_user") -> dict:
    """List available traces for a user (R5)."""
    return {"traces": list_traces(user_id)}


@app.get("/api/metrics")
def metrics(user_id: str = "", since: str = "") -> dict:
    """Aggregate key metrics from trace JSONL (R8).

    user_id 留空则聚合所有用户；since 为 ISO 时间字符串，仅统计其后的事件。
    """
    return read_metrics(user_id=user_id, since=since)


@app.get("/api/knowledgebase/files")
def knowledgebase_files(user_id: str = "web_user") -> list[dict]:
    """List raw knowledgebase files."""
    return list_knowledgebase_files(user_id)


@app.get("/api/knowledgebase/files/{name}")
def knowledgebase_file(name: str, user_id: str = "web_user") -> dict:
    """Return one raw knowledgebase markdown file."""
    result = read_knowledgebase_file(user_id, name)
    if result is None:
        raise HTTPException(status_code=404, detail="Knowledgebase file not found")
    return result


@app.post("/api/profile")
def profile(payload: ProfileRequest) -> dict[str, str]:
    """Create or update a user's profile."""
    profile = WellnessProfile(**payload.profile.model_dump())
    message = apply_profile(payload.user_id, profile)
    return {"message": message}


@app.post("/api/memory/clear")
def clear_memory(payload: UserScopedRequest) -> dict[str, str]:
    """Clear only the current user's memories."""
    return clear_user_memories(payload.user_id)


# ---------------------------------------------------------------- media (R9)
@app.post("/api/upload")
async def upload_media(
    user_id: str = Form("web_user"),
    file: UploadFile = File(...),
) -> dict:
    """Upload one image and return its ``file_id`` (R9, 方案 §2.5).

    多模态关闭时返回 501。图片原始字节落盘到 ``MediaStore``，
    调用方之后只需在 ``/api/chat`` 的 ``attachments`` 里带 ``file_id``——
    base64 永远不会进入请求体、messages 或 SessionStore。
    """
    if not vision_enabled():
        raise HTTPException(
            status_code=501,
            detail="多模态未启用。请设置 WELLNESS_VLM_ENABLED=true 后重试。",
        )
    data = await file.read()
    try:
        ref = get_media_store().save(
            user_id, file.filename or "upload", data, declared_mime=file.content_type
        )
    except UnsupportedMediaTypeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except MediaTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except MediaStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "file_id": ref.file_id,
        "mime": ref.mime,
        "size_bytes": ref.size_bytes,
        "width": ref.width,
        "height": ref.height,
        "preview_url": ref.preview_url,
    }


@app.get("/api/media/{file_id}")
def get_media(file_id: str, user_id: str = "web_user") -> Response:
    """Serve an uploaded image (前端气泡渲染用)，按 ``user_id`` 校验归属。"""
    store = get_media_store()
    ref = store.get(file_id, user_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="Media not found")
    data = store.read_bytes(file_id, user_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return Response(
        content=data,
        media_type=ref.mime,
        headers={"Cache-Control": "private, max-age=86400"},
    )


# ---------------------------------------------------------------- advice (R11)
@app.get("/api/advice/list")
def advice_list(user_id: str = "web_user", status: str = "", limit: int = 50) -> dict:
    """List recorded advice with follow-up state (R11, 方案 §3.6)."""
    return list_advice(user_id, status=status, limit=limit)


@app.post("/api/advice/feedback")
def advice_feedback(payload: AdviceFeedbackRequest) -> dict:
    """Record execution feedback for a past advice (R11).

    与模型调用 ``advice_feedback`` 工具走**同一条路径**，因此状态机更新与
    episodic 记忆写入两个副作用完全一致。
    """
    result = submit_advice_feedback(
        payload.user_id,
        advice_id=payload.advice_id,
        status=payload.status,
        note=payload.note,
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


# ---------------------------------------------------------------- push (R10)
@app.get("/api/push/plan")
def push_plan_get(user_id: str = "web_user") -> dict:
    """Read the user's push plan (R10, 方案 §4.2)."""
    return get_push_plan(user_id)


@app.post("/api/push/plan")
def push_plan_set(payload: PushPlanRequest) -> dict:
    """Update push plan: on/off, time, kinds (R10)."""
    return update_push_plan(
        payload.user_id,
        enabled=payload.enabled,
        daily_at=payload.daily_at,
        timezone=payload.timezone,
        kinds=payload.kinds,
    )


@app.post("/api/push/run-now")
def push_run_now(payload: UserScopedRequest) -> dict:
    """Generate and deliver one push immediately (R10, demo 必需).

    刻意**不检查**总开关与 ``plan.enabled``：否则演示时得先开全局调度再等到点。
    """
    return run_push_now(payload.user_id)


@app.get("/api/push/inbox")
def push_inbox(
    user_id: str = "web_user", unread_only: bool = False, limit: int = 50
) -> dict:
    """Read the push inbox with unread count (R10, 方案 §4.4)."""
    return list_push_inbox(user_id, unread_only=unread_only, limit=limit)


@app.post("/api/push/read")
def push_read(payload: PushReadRequest) -> dict:
    """Mark one push (or all) as read (R10)."""
    if not payload.push_id and not payload.all_read:
        raise HTTPException(
            status_code=400, detail="push_id is required unless all_read is true"
        )
    return mark_push_read(
        payload.user_id, push_id=payload.push_id, all_read=payload.all_read
    )


frontend_dist = get_frontend_dist()
if frontend_dist.exists():
    assets_dir = frontend_dist / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> FileResponse:
        """Serve the React SPA if it has been built."""
        index_file = frontend_dist / "index.html"
        if not index_file.exists():
            raise HTTPException(status_code=404, detail="Frontend build not found")
        return FileResponse(index_file)
