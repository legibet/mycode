"""Session management API endpoints."""

from __future__ import annotations

import os

from fastapi import APIRouter

from mycode.core.config import get_settings, resolve_provider
from mycode.server.deps import store
from mycode.server.schemas import SessionCreateRequest

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("")
async def create_session(req: SessionCreateRequest):
    cwd = os.path.abspath(req.cwd or os.getcwd())
    settings = get_settings(cwd)
    resolved = resolve_provider(settings, model=req.model)
    api_base = req.api_base or resolved.api_base
    return await store.create_session(req.title, model=resolved.model, cwd=cwd, api_base=api_base)


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
