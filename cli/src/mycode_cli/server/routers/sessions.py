"""Session management API endpoints."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as PathParam

from mycode.messages import ConversationMessage
from mycode_cli.runtime import load_session_cost
from mycode_cli.server.deps import RunManagerDep, StoreDep, resolve_workspace_cwd
from mycode_cli.server.schemas import SessionCreateRequest, StatusResponse

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _redact_document_data(messages: list[ConversationMessage]) -> list[dict[str, Any]]:
    redacted: list[dict[str, Any]] = []
    for message in messages:
        content = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "document" and block.get("data"):
                block = {**block, "data": ""}
            content.append(block)
        redacted.append({**message, "content": content})
    return redacted


@router.post("")
async def create_session(req: SessionCreateRequest, store: StoreDep) -> dict[str, Any]:
    cwd = resolve_workspace_cwd(req.cwd)
    session_id = uuid4().hex
    data = await store.create_session(session_id, cwd=cwd)
    return {"session": data["session"], "messages": data["messages"]}


@router.get("")
async def list_sessions(
    store: StoreDep,
    runs: RunManagerDep,
    cwd: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    sessions = await store.list_sessions(cwd=cwd)
    for session in sessions:
        session_id = str(session["id"])
        session["is_running"] = await runs.has_active_run(session_id)
    return {"sessions": sessions}


@router.get("/{session_id}")
async def load_session(
    session_id: Annotated[str, PathParam(min_length=1)], store: StoreDep, runs: RunManagerDep
) -> dict[str, Any]:
    """Load a session, overlaying any active in-memory run state."""

    data = await store.load_session(session_id)
    # Folded from the raw JSONL: counts everything persisted so far, including
    # rewound-away turns. During an active run the SSE usage events supersede it.
    session_cost = await load_session_cost(store, session_id)
    active = await runs.snapshot_session(session_id)
    if active:
        return {
            "session": data["session"] if data else None,
            "messages": _redact_document_data(active["messages"]),
            "session_cost": session_cost,
            "active_run": active["run"],
            "pending_events": active["pending_events"],
        }

    if data is None:
        return {"session": None, "messages": [], "session_cost": None, "active_run": None, "pending_events": []}

    return {
        "session": data["session"],
        "messages": _redact_document_data(data["messages"]),
        "session_cost": session_cost,
        "active_run": None,
        "pending_events": [],
    }


@router.delete("/{session_id}")
async def delete_session(
    session_id: Annotated[str, PathParam(min_length=1)], store: StoreDep, runs: RunManagerDep
) -> StatusResponse:
    async with runs.session_operation(session_id):
        if await runs.has_active_run(session_id):
            raise HTTPException(status_code=409, detail="session has a running task")
        await store.delete_session(session_id)
    return StatusResponse(status="ok")


@router.post("/{session_id}/clear")
async def clear_session(
    session_id: Annotated[str, PathParam(min_length=1)], store: StoreDep, runs: RunManagerDep
) -> StatusResponse:
    async with runs.session_operation(session_id):
        if await runs.has_active_run(session_id):
            raise HTTPException(status_code=409, detail="session has a running task")
        await store.clear_session(session_id)
    return StatusResponse(status="ok")
