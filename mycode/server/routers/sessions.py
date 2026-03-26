"""Session management API endpoints."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from mycode.core.config import get_settings, resolve_provider
from mycode.server.deps import RunManagerDep, StoreDep
from mycode.server.schemas import SessionCreateRequest

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("")
async def create_session(req: SessionCreateRequest, store: StoreDep):
    cwd = os.path.abspath(req.cwd or os.getcwd())
    settings = get_settings(cwd)
    resolved = resolve_provider(settings, provider_name=req.provider, model=req.model, api_base=req.api_base)
    return await store.create_session(
        req.title,
        provider=resolved.provider,
        model=resolved.model,
        cwd=cwd,
        api_base=resolved.api_base,
    )


@router.get("")
async def list_sessions(store: StoreDep, runs: RunManagerDep, cwd: str | None = None):
    sessions = await store.list_sessions(cwd=cwd)
    for session in sessions:
        session["is_running"] = await runs.has_active_run(session.get("id", ""))
    return {"sessions": sessions}


@router.get("/{session_id}")
async def load_session(session_id: str, store: StoreDep, runs: RunManagerDep):
    active = await runs.snapshot_session(session_id)
    if active:
        data = await store.load_session(session_id)
        session = data.get("session") if data else None
        return {
            "session": session,
            "messages": active["messages"],
            "active_run": active["run"],
            "pending_events": active["pending_events"],
        }

    data = await store.load_session(session_id)
    if not data:
        return {"session": None, "messages": [], "active_run": None, "pending_events": []}
    return {**data, "active_run": None, "pending_events": []}


@router.delete("/{session_id}")
async def delete_session(session_id: str, store: StoreDep, runs: RunManagerDep):
    if await runs.has_active_run(session_id):
        raise HTTPException(status_code=409, detail="session has a running task")
    await store.delete_session(session_id)
    return {"status": "ok"}


@router.post("/{session_id}/clear")
async def clear_session(session_id: str, store: StoreDep, runs: RunManagerDep):
    if await runs.has_active_run(session_id):
        raise HTTPException(status_code=409, detail="session has a running task")
    await store.clear_session(session_id)
    return {"status": "ok"}
