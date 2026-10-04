"""Session management API endpoints."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as PathParam

from mycode.messages import ConversationMessage
from mycode_cli.server.deps import RunManagerDep, StoreDep, resolve_workspace_cwd
from mycode_cli.server.schemas import SessionCreateRequest, StatusResponse
from mycode_cli.sessions import SessionTotals

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
    session = await store.create_session(session_id, cwd=cwd)
    return {"session": session, "messages": []}


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


@router.get("/search")
async def search_sessions(
    store: StoreDep,
    runs: RunManagerDep,
    q: Annotated[str, Query(min_length=1)],
    cwd: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
) -> dict[str, Any]:
    results = await store.search_sessions(q, cwd=cwd, limit=limit)
    for result in results:
        session = result["session"]
        session["is_running"] = await runs.has_active_run(str(session["id"]))
    return {"results": results}


@router.get("/{session_id}")
async def load_session(
    session_id: Annotated[str, PathParam(min_length=1)], store: StoreDep, runs: RunManagerDep
) -> dict[str, Any]:
    """Load a session, overlaying any active in-memory run state."""

    async with runs.session_operation(session_id):
        active = await runs.snapshot_session(session_id)
        jobs = runs.live_jobs(session_id)
        if active:
            return {
                "session": await store.load_metadata(session_id),
                "messages": _redact_document_data(active["messages"]),
                **active["totals"].payload(),
                "active_run": active["run"],
                "pending_events": active["pending_events"],
                "pending": {kind: _redact_document_data(messages) for kind, messages in active["pending"].items()},
                "jobs": jobs,
            }

        data = await store.load_session(session_id)

    if data is None:
        return {
            "session": None,
            "messages": [],
            **SessionTotals().payload(),
            "active_run": None,
            "pending_events": [],
            "pending": {"steers": [], "queue": []},
            "jobs": jobs,
        }

    return {
        "session": data["session"],
        "messages": _redact_document_data(data["messages"]),
        **data["totals"].payload(),
        "active_run": None,
        "pending_events": [],
        "pending": {"steers": [], "queue": []},
        "jobs": jobs,
    }


@router.delete("/{session_id}/jobs/{tool_use_id}")
async def stop_job(
    session_id: Annotated[str, PathParam(min_length=1)],
    tool_use_id: Annotated[str, PathParam(min_length=1)],
    runs: RunManagerDep,
) -> StatusResponse:
    """Kill a running background command; its result still reaches the model, with the signal's exit code."""

    if not runs.kill_job(session_id, tool_use_id):
        raise HTTPException(status_code=404, detail="background job not found")
    return StatusResponse(status="ok")


@router.delete("/{session_id}")
async def delete_session(
    session_id: Annotated[str, PathParam(min_length=1)], store: StoreDep, runs: RunManagerDep
) -> StatusResponse:
    async with runs.session_operation(session_id):
        if await runs.has_active_run(session_id):
            raise HTTPException(status_code=409, detail="session has a running task")
        await runs.close_jobs(session_id)
        await store.delete_session(session_id)
    return StatusResponse(status="ok")
