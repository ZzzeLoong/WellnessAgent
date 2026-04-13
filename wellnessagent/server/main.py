"""FastAPI application for the wellness agent web UI."""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from ..schemas import WellnessProfile
from .deps import (
    apply_profile,
    clear_user_memories,
    ensure_knowledgebase_seeded,
    get_agent,
    get_frontend_dist,
    list_knowledgebase_files,
    read_knowledgebase_file,
)
from .schemas import ChatRequest, ProfileRequest, UserScopedRequest


load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


app = FastAPI(title="WellnessAgent API", version="0.1.0")

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
    return agent.chat_with_trace(payload.message)


@app.get("/api/state")
def state(user_id: str = "web_user") -> dict:
    """Return the current structured state for a user."""
    ensure_knowledgebase_seeded(user_id)
    return get_agent(user_id).get_state_dict()


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
