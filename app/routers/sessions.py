"""Session management API endpoints."""

from __future__ import annotations

import os

from fastapi import APIRouter

from app.config import get_settings
from app.schemas import SessionCreateRequest
from app.session import SessionStore

router = APIRouter(prefix="/sessions", tags=["sessions"])
store = SessionStore()


@router.post("")
async def create_session(req: SessionCreateRequest):
    settings = get_settings()
    cfg = settings.active_provider
    model = (
        req.model or settings.default_model or (cfg.models[0] if cfg and cfg.models else None) or "claude-sonnet-4-5"
    )
    cwd = req.cwd or os.getcwd()
    api_base = req.api_base or (cfg.base_url if cfg else None)
    return await store.create_session(req.title, model=model, cwd=cwd, api_base=api_base)


@router.get("")
async def list_sessions(cwd: str | None = None):
    return {"sessions": await store.list_sessions(cwd=cwd)}


@router.get("/{session_id}")
async def load_session(session_id: str):
    data = await store.load_session(session_id)
    if not data:
        return {"session": None, "messages": []}
    return data


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    await store.delete_session(session_id)
    return {"status": "ok"}


@router.post("/{session_id}/clear")
async def clear_session(session_id: str):
    await store.clear_session(session_id)
    return {"status": "ok"}
