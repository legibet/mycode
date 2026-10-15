"""Chat and run streaming API."""

from __future__ import annotations

import asyncio
import io
import json
from base64 import b64encode
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse

from mycode.attachments import (
    Attachment,
    build_attachment_blocks,
    detect_document_mime_type,
    detect_image_mime_type,
)
from mycode.compact import has_compactable_history
from mycode.messages import (
    ContentBlock,
    ConversationMessage,
    build_message,
    document_block,
    image_block,
    text_block,
)
from mycode.models import ModelMetadata
from mycode_cli.background import BackgroundJobs
from mycode_cli.config import (
    ResolvedProvider,
    Settings,
    WebConfig,
    get_settings,
    resolve_provider,
)
from mycode_cli.permissions import ToolReviewCallback, ToolReviewDecision, ToolReviewRequest
from mycode_cli.runtime import build_agent
from mycode_cli.server.deps import RunManagerDep, StoreDep, resolve_workspace_cwd
from mycode_cli.server.run_manager import RunAgent, RunManager
from mycode_cli.server.schemas import (
    CancelRunResponse,
    ChatRequest,
    ChatResponse,
    CompactRequest,
    DecideRequest,
    PendingInputRequest,
    RunInfo,
    RunResponse,
    StatusResponse,
    UserInputRequest,
)
from mycode_cli.sessions import SessionData, SessionStore, display_text
from mycode_cli.system_prompt import build_skill_snapshot_blocks
from mycode_cli.tools import read_text_window
from mycode_cli.workspace import CliDeps, resolve_path

router = APIRouter()


def _resolve_workspace_attachment_path(rel_path: str, *, cwd: str) -> Path:
    base = resolve_path(".", cwd=cwd)
    path = resolve_path(rel_path, cwd=cwd)
    if not path.is_relative_to(base):
        raise HTTPException(status_code=400, detail=f"path outside workspace: {rel_path}")
    return path


def _read_workspace_text_attachment(rel_path: str, *, name: str | None, cwd: str) -> list[ContentBlock]:
    """Read a workspace text file selected via the @ menu into a `<file>` block, bounded like ``read``.

    Re-validates the path at send time: it must resolve inside ``cwd`` (guards
    against a symlink swapped in after selection), be a regular file, and be
    UTF-8 text (image/PDF go through image/document path blocks instead).
    """
    path = _resolve_workspace_attachment_path(rel_path, cwd=cwd)
    if not path.is_file():
        raise HTTPException(status_code=400, detail=f"text file not found: {rel_path}")
    if detect_image_mime_type(path) or detect_document_mime_type(path):
        raise HTTPException(status_code=400, detail=f"not a text file: {rel_path}")
    try:
        with path.open(encoding="utf-8") as file:
            window = read_text_window(file)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"not UTF-8 text: {rel_path}") from exc
    return build_attachment_blocks([Attachment.text(window.render(path), name=name or rel_path)])


def _write_uploads(uploads: dict[Path, str]) -> None:
    for path, text in uploads.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _remove_uploads(uploads: dict[Path, str]) -> None:
    """Undo ``_write_uploads`` for a message the run refused."""

    for path in uploads:
        path.unlink(missing_ok=True)


async def _build_user_message(chat: UserInputRequest, deps: CliDeps) -> tuple[ConversationMessage, dict[Path, str]]:
    """Build the user message and the uploads to save before it is sent.

    A text upload that does not fit one read window is shown truncated, and its
    full text is returned for ``tool_output_dir`` so the model can read the rest.
    The caller writes it once the request has passed validation.
    """

    cwd = str(deps.cwd)
    uploads: dict[Path, str] = {}
    if not chat.input:
        text = str(chat.message or "").strip()
        snapshots = await asyncio.to_thread(build_skill_snapshot_blocks, text, cwd)
        return build_message("user", [*snapshots, text_block(text)]), uploads

    blocks: list[ContentBlock] = []
    visible_text: list[str] = []
    for block in chat.input:
        if block.type == "text":
            if block.path:
                blocks.extend(
                    await asyncio.to_thread(_read_workspace_text_attachment, block.path, name=block.name, cwd=cwd)
                )
                continue
            text = block.text or ""
            if block.is_attachment:
                name = str(block.name or "attached-file")
                window = read_text_window(io.StringIO(text, newline=None))
                upload_path = deps.tool_output_dir / f"upload-{uuid4().hex}"
                if window.partial:
                    uploads[upload_path] = text
                blocks.extend(build_attachment_blocks([Attachment.text(window.render(upload_path), name=name)]))
            elif text:
                visible_text.append(text)
                blocks.append(text_block(text))
            continue

        # block.type is "image" or "document" from here on.
        factory = document_block if block.type == "document" else image_block

        # Inline base64 from the web client.
        if block.data:
            mime_type = block.mime_type or "application/pdf"
            default_name = "document.pdf" if block.type == "document" else "image"
            blocks.append(factory(block.data, mime_type=mime_type, name=block.name or default_name))
            continue

        # Server-side path: read the bytes and validate the sniffed MIME matches
        # the declared block.type so a request for an "image" can't ship a PDF.
        rel_path = cast(str, block.path)
        path = (
            _resolve_workspace_attachment_path(rel_path, cwd=cwd)
            if block.is_attachment
            else resolve_path(rel_path, cwd=cwd)
        )
        if not path.is_file():
            raise HTTPException(status_code=400, detail=f"{block.type} file not found: {block.path}")
        detect = detect_image_mime_type if block.type == "image" else detect_document_mime_type
        mime_type = block.mime_type or detect(path)
        if not mime_type or (block.type == "document" and mime_type != "application/pdf"):
            raise HTTPException(status_code=400, detail=f"unsupported {block.type} file: {block.path}")
        data = b64encode(await asyncio.to_thread(path.read_bytes)).decode("utf-8")
        blocks.append(factory(data, mime_type=mime_type, name=block.name or path.name))

    if not blocks:
        raise HTTPException(status_code=400, detail="input must include at least one non-empty block")
    snapshots = await asyncio.to_thread(build_skill_snapshot_blocks, "\n".join(visible_text), cwd)
    blocks[:0] = snapshots
    return build_message("user", blocks), uploads


def _check_input_support(message: ConversationMessage, model: ModelMetadata | RunAgent) -> None:
    content_types = {block["type"] for block in message["content"]}
    if "image" in content_types and not model.supports_image_input:
        raise HTTPException(status_code=400, detail="current model does not support image input")
    if "document" in content_types and not model.supports_pdf_input:
        raise HTTPException(status_code=400, detail="current model does not support PDF input")


def _validate_rewind_request(data: SessionData | None, rewind_to: int) -> None:
    if data is None:
        raise HTTPException(status_code=400, detail="rewind_to requires an existing session")

    messages = data["messages"]
    if not (0 <= rewind_to < len(messages)):
        raise HTTPException(
            status_code=400,
            detail=f"rewind_to must reference a visible message index between 0 and {len(messages) - 1}",
        )

    target = messages[rewind_to]
    has_user_content = any(
        (block["type"] == "text" and block["text"] and "job" not in block.get("meta", {}))
        or block["type"] in ("image", "document")
        for block in target["content"]
    )
    if target["role"] != "user" or not has_user_content:
        raise HTTPException(status_code=400, detail="rewind_to must reference a real user message")


async def _ensure_idle(runs: RunManager, session_id: str) -> None:
    """Refuse a new run while the session has one.

    Called under ``runs.session_operation``, which every run start holds, so
    the session stays idle until the caller starts its run.
    """

    if active := await runs.active_run_info(session_id):
        raise HTTPException(status_code=409, detail={"message": "session already has a running task", "run": active})


@router.post("/chat")
async def chat(chat: ChatRequest, store: StoreDep, runs: RunManagerDep) -> ChatResponse:
    cwd = resolve_workspace_cwd(chat.cwd)
    settings = await asyncio.to_thread(get_settings, cwd)
    resolved = resolve_provider(settings, provider_name=chat.provider, model=chat.model)
    try:
        request_provider = resolved.with_reasoning_effort(chat.reasoning_effort)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session_id = chat.session_id or "default"
    jobs = runs.jobs_for(session_id)
    user_message, uploads = await _build_user_message(
        chat, CliDeps.for_session(cwd=cwd, data_dir=store.data_dir, session_id=session_id, web=settings.web)
    )

    # Capability check before any disk mutation — a failed check must not
    # leave an empty session on disk or land a premature rewind marker.
    _check_input_support(user_message, resolved.metadata)

    async with runs.session_operation(session_id):
        await _ensure_idle(runs, session_id)

        if chat.rewind_to is not None:
            _validate_rewind_request(await store.load_session(session_id), chat.rewind_to)

        # All validation passed. Save uploads, land the rewind marker (if any)
        # and register the user turn in the catalog: first turn creates the
        # entry and sets the title, later turns bump updated_at.
        await asyncio.to_thread(_write_uploads, uploads)
        if chat.rewind_to is not None:
            # A result for a tool call that is no longer in the history would confuse the model.
            await jobs.close()
            await store.append_rewind(session_id, chat.rewind_to)
        session = await store.record_user_turn(
            session_id,
            cwd=cwd,
            text=display_text(user_message),
        )

        async def review(request: ToolReviewRequest) -> ToolReviewDecision:
            # Bridge before_tool review waits to SSE events on the active run.
            return await runs.request_decision(
                session_id=session_id,
                tool_call_id=request.tool_call_id,
                tool_name=request.tool_name,
                preview=request.preview,
            )

        agent = await asyncio.to_thread(
            build_agent,
            store=store,
            settings=settings,
            resolved_provider=request_provider,
            session_id=session_id,
            review=review,
            jobs=jobs,
        )
        # A wake reuses this request's configuration until the next request replaces it.
        jobs.deliver = partial(
            _deliver_job_results,
            runs=runs,
            store=store,
            jobs=jobs,
            session_id=session_id,
            settings=settings,
            resolved_provider=request_provider,
            review=review,
        )

        session_base = await store.load_totals(session_id)
        # The user's message ends a Stop and carries the results that waited
        # for it. Taken last: a run that fails to start leaves nothing in flight.
        jobs.suspended = False
        user_message["content"] = [*jobs.take_pending(), *user_message["content"]]
        run = await runs.start_run(
            session_id=session_id,
            cwd=cwd,
            user_message=user_message,
            base_messages=agent.messages,
            agent=agent,
            session_base=session_base,
        )

    return ChatResponse(run=RunInfo.model_validate(run), session=session, message=user_message)


async def _deliver_job_results(
    *,
    runs: RunManager,
    store: SessionStore,
    jobs: BackgroundJobs,
    session_id: str,
    settings: Settings,
    resolved_provider: ResolvedProvider,
    review: ToolReviewCallback,
) -> None:
    """Hand the session's finished background results to a run.

    A running chat takes them as a steer, or as its next turn when it is
    finishing. An idle session wakes with a turn made of the results, unless a
    Stop suspended wakes. A compact run or a closing chat run takes nothing;
    the run manager calls again when that run ends.
    """

    async with runs.session_operation(session_id):
        state = await runs.get_active_run(session_id)
        if state is None:
            if jobs.suspended or not jobs.deliverable():
                return
            # A wake that cannot start holds the next one, like one that never committed.
            try:
                agent = await asyncio.to_thread(
                    build_agent,
                    store=store,
                    settings=settings,
                    resolved_provider=resolved_provider,
                    session_id=session_id,
                    review=review,
                    jobs=jobs,
                )
                await store.touch(session_id)
                session_base = await store.load_totals(session_id)
            except Exception:
                jobs.suspended = True
                raise
            await runs.start_run(
                session_id=session_id,
                cwd=settings.cwd,
                user_message=build_message("user", jobs.take_pending()),
                base_messages=agent.messages,
                agent=agent,
                session_base=session_base,
                wake=True,
            )
        elif state.kind == "chat":
            blocks = jobs.take_pending()
            if blocks:
                message = build_message("user", blocks)
                if not state.agent.steer(message):
                    # Refused by both: the blocks stay in flight until this run's reconcile.
                    await runs.enqueue(state, message)


@router.get("/events")
async def stream_server_events(req: Request, runs: RunManagerDep) -> StreamingResponse:
    """Announce run starts and ends across sessions; no replay, clients resync on connect."""

    async def events() -> AsyncIterator[str]:
        async with runs.subscribe() as queue:
            while not await req.is_disconnected():
                try:
                    async with asyncio.timeout(15):
                        payload = await queue.get()
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: Annotated[str, PathParam(min_length=1)],
    req: Request,
    runs: RunManagerDep,
    after: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    if not await runs.get_run(run_id):
        raise HTTPException(status_code=404, detail="run not found")

    async def events() -> AsyncIterator[str]:
        async for payload in runs.stream_events(run_id, after):
            if await req.is_disconnected():
                return
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: Annotated[str, PathParam(min_length=1)], runs: RunManagerDep) -> CancelRunResponse:
    run = await runs.cancel_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return CancelRunResponse(status="ok", run=RunInfo.model_validate(run))


@router.post("/runs/{run_id}/decide")
async def decide_run(
    run_id: Annotated[str, PathParam(min_length=1)], body: DecideRequest, runs: RunManagerDep
) -> StatusResponse:
    resolved = await runs.resolve_decision(run_id, body.request_id, body.decision)
    if not resolved:
        raise HTTPException(status_code=404, detail="permission request not found")
    return StatusResponse(status="ok")


@router.post("/sessions/{session_id}/compact")
async def compact_session(
    session_id: Annotated[str, PathParam(min_length=1)],
    body: CompactRequest,
    store: StoreDep,
    runs: RunManagerDep,
) -> RunResponse:
    """Start a compact run: summarize the session and append one compact marker."""

    async with runs.session_operation(session_id):
        await _ensure_idle(runs, session_id)

        data = await store.load_session(session_id)
        if data is None:
            raise HTTPException(status_code=404, detail="session not found")
        if not has_compactable_history(data["messages"]):
            raise HTTPException(status_code=400, detail="nothing to compact")

        settings = await asyncio.to_thread(get_settings, data["session"]["cwd"])
        resolved = resolve_provider(settings, provider_name=body.provider, model=body.model)

        agent = await asyncio.to_thread(
            build_agent,
            store=store,
            settings=settings,
            resolved_provider=resolved,
            session_id=session_id,
        )

        run = await runs.start_compact(
            session_id=session_id,
            cwd=settings.cwd,
            base_messages=agent.messages,
            agent=agent,
            session_base=data["totals"],
            on_complete=store.touch,
        )

    return RunResponse(run=RunInfo.model_validate(run))


@router.post("/runs/{run_id}/steer")
async def steer_run(
    run_id: Annotated[str, PathParam(min_length=1)], body: PendingInputRequest, store: StoreDep, runs: RunManagerDep
) -> RunResponse:
    """Hand a user message to the running chat for its next step boundary."""

    state = await runs.get_run(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")
    message, uploads = await _build_user_message(
        body,
        CliDeps.for_session(cwd=state.cwd, data_dir=store.data_dir, session_id=state.session_id, web=WebConfig()),
    )
    message["meta"] = {"input_id": body.input_id}
    _check_input_support(message, state.agent)
    # Uploads are on disk before the model can see the message; a refusal removes them.
    await asyncio.to_thread(_write_uploads, uploads)
    if not state.agent.steer(message):
        await asyncio.to_thread(_remove_uploads, uploads)
        raise HTTPException(
            status_code=409,
            detail={"message": "run is not accepting steers", "run": state.info()},
        )
    return RunResponse(run=RunInfo.model_validate(state.info()))


@router.post("/sessions/{session_id}/queue")
async def queue_message(
    session_id: Annotated[str, PathParam(min_length=1)],
    body: PendingInputRequest,
    store: StoreDep,
    runs: RunManagerDep,
) -> RunResponse:
    """Queue a user message as the next turn of the session's running chat."""

    state = await runs.get_active_run(session_id)
    if state is None:
        raise HTTPException(status_code=409, detail={"message": "session has no running chat to queue on"})
    message, uploads = await _build_user_message(
        body, CliDeps.for_session(cwd=state.cwd, data_dir=store.data_dir, session_id=session_id, web=WebConfig())
    )
    message["meta"] = {"input_id": body.input_id}
    _check_input_support(message, state.agent)
    await asyncio.to_thread(_write_uploads, uploads)
    if not await runs.enqueue(state, message):
        await asyncio.to_thread(_remove_uploads, uploads)
        raise HTTPException(status_code=409, detail={"message": "session has no running chat to queue on"})
    await store.record_user_turn(
        session_id,
        cwd=state.cwd,
        text=display_text(message),
    )
    return RunResponse(run=RunInfo.model_validate(state.info()))


@router.delete("/sessions/{session_id}/queue/{input_id}")
async def remove_queued_message(
    session_id: Annotated[str, PathParam(min_length=1)],
    input_id: Annotated[str, PathParam(min_length=1)],
    runs: RunManagerDep,
) -> RunResponse:
    """Remove a queued message that has not been delivered."""

    run = await runs.remove_queued(session_id, input_id)
    if run is None:
        raise HTTPException(status_code=404, detail="queued message not found")
    return RunResponse(run=RunInfo.model_validate(run))


@router.post("/sessions/{session_id}/queue/{input_id}/steer")
async def steer_queued_message(
    session_id: Annotated[str, PathParam(min_length=1)],
    input_id: Annotated[str, PathParam(min_length=1)],
    runs: RunManagerDep,
) -> RunResponse:
    """Move a queued message into the current turn, keeping it as built."""

    state = await runs.get_active_run(session_id)
    moved = None if state is None else await runs.steer_queued(state, input_id)
    if state is None or moved is None:
        raise HTTPException(status_code=404, detail="queued message not found")
    if not moved:
        raise HTTPException(
            status_code=409,
            detail={"message": "run is not accepting steers", "run": state.info()},
        )
    return RunResponse(run=RunInfo.model_validate(state.info()))
