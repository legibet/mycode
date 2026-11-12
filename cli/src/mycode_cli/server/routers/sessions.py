"""Session management API endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from mycode.messages import ContentBlock, ConversationMessage
from mycode.session import apply_rewind
from mycode_cli.agent_tool import AGENT_INFO_FILE, read_agent_info
from mycode_cli.server.deps import LOCK_HOLDER, PathId, RunManagerDep, StoreDep, resolve_workspace_cwd
from mycode_cli.server.schemas import AgentSessionInfo, SessionCreateRequest, StatusResponse
from mycode_cli.sessions import SessionStore, SessionTotals, sum_session_totals

router = APIRouter(prefix="/sessions", tags=["sessions"])

LOG_CHUNK_BYTES = 64 * 1024
# How often a log is read again while its job runs.
LOG_POLL_SECONDS = 0.2


def _redact_document_data(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    redacted: list[ConversationMessage] = []
    for message in messages:
        content: list[ContentBlock] = []
        for block in message["content"]:
            if block["type"] == "document" and block["data"]:
                block = block | {"data": ""}
            content.append(block)
        redacted.append(message | {"content": content})
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
    return {"sessions": [{**session, "is_running": await runs.has_active_run(session["id"])} for session in sessions]}


@router.get("/search")
async def search_sessions(
    store: StoreDep,
    runs: RunManagerDep,
    q: Annotated[str, Query(min_length=1)],
    cwd: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
) -> dict[str, Any]:
    results = []
    for result in await store.search_sessions(q, cwd=cwd, limit=limit):
        session = result["session"]
        results.append({**result, "session": {**session, "is_running": await runs.has_active_run(session["id"])}})
    return {"results": results}


@router.get("/{session_id}")
async def load_session(session_id: PathId, store: StoreDep, runs: RunManagerDep) -> dict[str, Any]:
    """Load a session, overlaying any active in-memory run state."""

    async with runs.session_operation(session_id):
        jobs = runs.live_jobs(session_id)
        if active := await runs.snapshot_session(session_id):
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

    session, messages, totals = (
        (data["session"], data["messages"], data["totals"]) if data else (None, [], SessionTotals())
    )
    return {
        "session": session,
        "messages": _redact_document_data(messages),
        **totals.payload(),
        "active_run": None,
        "pending_events": [],
        "pending": {"steers": [], "queue": []},
        "jobs": jobs,
    }


@router.get("/{session_id}/agents/{child_id}")
async def load_agent_session(
    session_id: PathId, child_id: PathId, store: StoreDep, runs: RunManagerDep
) -> dict[str, Any]:
    """Load a subagent's session in the shape of ``load_session``, overlaying its running turn."""

    async with runs.session_operation(session_id):
        agents_dir = store.agents_dir(session_id)
        info = await asyncio.to_thread(read_agent_info, agents_dir / child_id / AGENT_INFO_FILE)
        if info is None:
            # Missing, or not written yet: the agent tool takes the child's lock first.
            raise HTTPException(status_code=404, detail="agent not found")
        # The child's directory exists, so the store creates nothing.
        children = SessionStore(data_dir=agents_dir)
        session = AgentSessionInfo.model_validate({**info, "id": child_id}).model_dump(exclude_none=True)
        if active := await runs.snapshot_session(f"{session_id}/{child_id}"):
            return {
                "session": session,
                "messages": _redact_document_data(active["messages"]),
                **active["totals"].payload(),
                "active_run": active["run"],
                "pending_events": active["pending_events"],
                "pending": {kind: _redact_document_data(messages) for kind, messages in active["pending"].items()},
                "jobs": [],
            }
        raw = await asyncio.to_thread(children.load_raw_messages_sync, child_id)

    return {
        "session": session,
        "messages": _redact_document_data(apply_rewind(raw)),
        **sum_session_totals(raw).payload(),
        "active_run": None,
        "pending_events": [],
        "pending": {"steers": [], "queue": []},
        "jobs": [],
    }


@router.get("/{session_id}/logs/{job_id}")
async def stream_log(
    session_id: PathId,
    job_id: PathId,
    req: Request,
    store: StoreDep,
    runs: RunManagerDep,
    after: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """Stream the log of the session's background command ``job_id`` from byte ``after``, following it while the job runs."""

    try:
        log = (store.tool_output_dir(session_id) / f"bash-{job_id}.log").open("rb")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="log not found") from None

    async def follow() -> AsyncIterator[bytes]:
        with log:
            log.seek(after)
            while True:
                # Checked before the read: once the job is gone, its last
                # output is in the file and this read goes to its end.
                running = runs.has_job(session_id, job_id)
                if chunk := await asyncio.to_thread(log.read, LOG_CHUNK_BYTES):
                    yield chunk
                elif not running or await req.is_disconnected():
                    return
                else:
                    await asyncio.sleep(LOG_POLL_SECONDS)

    return StreamingResponse(
        follow(), media_type="text/plain", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.delete("/{session_id}/jobs/{job_id}")
async def stop_job(
    session_id: PathId,
    job_id: PathId,
    runs: RunManagerDep,
) -> StatusResponse:
    """Stop a running background job, a command or an agent; its result still reaches the model."""

    if not runs.kill_job(session_id, job_id):
        raise HTTPException(status_code=404, detail="background job not found")
    return StatusResponse(status="ok")


@router.delete("/{session_id}")
async def delete_session(session_id: PathId, store: StoreDep, runs: RunManagerDep) -> StatusResponse:
    async with runs.session_operation(session_id):
        if await runs.has_active_run(session_id):
            raise HTTPException(status_code=409, detail="session has a running task")
        with await asyncio.to_thread(store.lock, session_id, LOCK_HOLDER):
            await runs.close_jobs(session_id)
            await store.delete_session(session_id)
    return StatusResponse(status="ok")
