"""Session management API endpoints."""

from __future__ import annotations

import os
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from mycode_cli.server.deps import RunManagerDep, StoreDep
from mycode_cli.server.schemas import SessionCreateRequest

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("")
async def create_session(req: SessionCreateRequest, store: StoreDep):
    cwd = os.path.abspath(req.cwd or os.getcwd())
    session_id = uuid4().hex
    return await store.create_session(session_id, cwd=cwd)


@router.get("")
async def list_sessions(store: StoreDep, runs: RunManagerDep, cwd: str | None = None):
    sessions = await store.list_sessions(cwd=cwd)
    for session in sessions:
        session_id = str(session.get("id") or "")
        session["is_running"] = await runs.has_active_run(session_id)
    return {"sessions": sessions}


@router.get("/{session_id}")
async def load_session(session_id: str, store: StoreDep, runs: RunManagerDep):
    """Load a session, overlaying any active in-memory run state."""

    data = await store.load_session(session_id)
    session = data.get("session") if data else None
    active = await runs.snapshot_session(session_id)
    if active:
        return {
            "session": session,
            "messages": active["messages"],
            "active_run": active["run"],
            "pending_events": active["pending_events"],
        }

    if not data:
        return {"session": None, "messages": [], "active_run": None, "pending_events": []}

    return {
        "session": session,
        "messages": data.get("messages") or [],
        "active_run": None,
        "pending_events": [],
    }


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
