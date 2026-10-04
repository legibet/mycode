"""Chat and run streaming API."""

from __future__ import annotations

import asyncio
import io
import json
import os
from base64 import b64encode
from collections.abc import AsyncIterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Annotated, Any, cast
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
from mycode.compact import DEFAULT_COMPACT_THRESHOLD, has_compactable_history
from mycode.messages import (
    ConversationMessage,
    build_message,
    document_block,
    flatten_message_text,
    image_block,
    text_block,
)
from mycode.models import ModelMetadata
from mycode_cli.background import BackgroundJobs
from mycode_cli.config import (
    ResolvedProvider,
    Settings,
    get_settings,
    normalize_reasoning_effort,
    provider_models,
    resolve_configured_model_metadata,
    resolve_provider,
    resolve_provider_choices,
)
from mycode_cli.permissions import ToolReviewCallback, ToolReviewDecision, ToolReviewRequest
from mycode_cli.runtime import build_agent, load_session_totals
from mycode_cli.server.deps import RunManagerDep, StoreDep, resolve_workspace_cwd
from mycode_cli.server.run_manager import ActiveRunError, RunAgent, RunManager
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
    StreamEvent,
    UserInputRequest,
)
from mycode_cli.sessions import SessionStore
from mycode_cli.system_prompt import build_skill_snapshot_blocks, discover_slash_skills
from mycode_cli.tools import read_text_window
from mycode_cli.workspace import CliDeps, resolve_path

router = APIRouter()


def _resolve_workspace_attachment_path(rel_path: str, *, cwd: str) -> Path:
    base = resolve_path(".", cwd=cwd)
    path = resolve_path(rel_path, cwd=cwd)
    if not path.is_relative_to(base):
        raise HTTPException(status_code=400, detail=f"path outside workspace: {rel_path}")
    return path


def _read_workspace_text_attachment(rel_path: str, *, name: str | None, cwd: str) -> list[dict[str, Any]]:
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

    blocks: list[dict[str, Any]] = []
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
    content_types = {b.get("type") for b in (message.get("content") or []) if isinstance(b, dict)}
    if "image" in content_types and not model.supports_image_input:
        raise HTTPException(status_code=400, detail="current model does not support image input")
    if "document" in content_types and not model.supports_pdf_input:
        raise HTTPException(status_code=400, detail="current model does not support PDF input")


def _validate_rewind_request(
    *,
    session: dict[str, Any] | None,
    messages: list[ConversationMessage],
    rewind_to: int,
) -> None:
    if session is None:
        raise HTTPException(status_code=400, detail="rewind_to requires an existing session")

    if not (0 <= rewind_to < len(messages)):
        raise HTTPException(
            status_code=400,
            detail=f"rewind_to must reference a visible message index between 0 and {len(messages) - 1}",
        )

    target = messages[rewind_to]
    raw_blocks = target.get("content")
    blocks = raw_blocks if isinstance(raw_blocks, list) else []
    has_user_content = any(
        isinstance(block, dict)
        and (
            (block.get("type") == "text" and block.get("text") and "job" not in (block.get("meta") or {}))
            or block.get("type") in {"image", "document"}
        )
        for block in blocks
    )
    if target.get("role") != "user" or not has_user_content:
        raise HTTPException(status_code=400, detail="rewind_to must reference a real user message")


@router.post("/chat")
async def chat(chat: ChatRequest, store: StoreDep, runs: RunManagerDep) -> ChatResponse:
    cwd = resolve_workspace_cwd(chat.cwd)
    settings = await asyncio.to_thread(get_settings, cwd)
    resolved = resolve_provider(
        settings,
        provider_name=chat.provider,
        model=chat.model,
        api_key=chat.api_key,
        api_base=chat.api_base,
    )
    session_id = chat.session_id or "default"
    jobs = runs.jobs_for(session_id)
    user_message, uploads = await _build_user_message(
        chat, CliDeps.for_session(cwd=cwd, data_dir=store.data_dir, session_id=session_id)
    )

    # Capability check before any disk mutation — a failed check must not
    # leave an empty session on disk or land a premature rewind marker.
    model_config = resolved.model_config
    model_meta = resolve_configured_model_metadata(
        provider=resolved.provider,
        model=resolved.model,
        model_config=model_config,
    )
    _check_input_support(user_message, model_meta)

    reasoning_effort = resolved.reasoning_effort
    if "reasoning_effort" in chat.model_fields_set:
        try:
            reasoning_effort = normalize_reasoning_effort(chat.reasoning_effort)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if reasoning_effort is not None:
            if not resolved.supports_reasoning_effort:
                raise HTTPException(
                    status_code=400,
                    detail=f"provider {resolved.provider!r} does not support reasoning effort",
                )
            if not resolved.reasoning_efforts:
                raise HTTPException(
                    status_code=400,
                    detail=f"model {resolved.model!r} does not support reasoning effort",
                )
            if reasoning_effort not in resolved.reasoning_efforts:
                supported = ", ".join(resolved.reasoning_efforts)
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"reasoning effort {reasoning_effort!r} is not supported by model {resolved.model!r}; "
                        f"supported efforts: {supported}"
                    ),
                )

    request_provider = replace(resolved, reasoning_effort=reasoning_effort)

    async with runs.session_operation(session_id):
        active = await runs.active_run_info(session_id)
        if active:
            raise HTTPException(
                status_code=409,
                detail={"message": "session already has a running task", "run": active},
            )

        if chat.rewind_to is not None:
            data = await store.load_session(session_id)
            session = cast(dict[str, Any] | None, data["session"] if data else None)
            existing_messages = data["messages"] if data else []
            _validate_rewind_request(session=session, messages=existing_messages, rewind_to=chat.rewind_to)

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
            text=flatten_message_text(user_message, include_thinking=False),
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
            cwd=cwd,
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
            cwd=cwd,
            settings=settings,
            resolved_provider=request_provider,
            review=review,
        )

        session_base = await load_session_totals(store, session_id)
        # The user's message ends a Stop and carries the results that waited
        # for it. Taken last: a run that fails to start leaves nothing in flight.
        jobs.suspended = False
        user_message["content"] = [*jobs.take_pending(), *user_message["content"]]
        try:
            run = await runs.start_run(
                session_id=session_id,
                cwd=cwd,
                user_message=user_message,
                base_messages=agent.messages,
                agent=agent,
                session_base=session_base,
            )
        except ActiveRunError as exc:
            existing = await runs.get_run(exc.run_id)
            detail: dict[str, Any] = {"message": "session already has a running task"}
            if existing:
                detail["run"] = existing.info()
            raise HTTPException(status_code=409, detail=detail) from exc

    return ChatResponse(run=RunInfo.model_validate(run), session=session, message=user_message)


async def _deliver_job_results(
    *,
    runs: RunManager,
    store: SessionStore,
    jobs: BackgroundJobs,
    session_id: str,
    cwd: str,
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
                    cwd=cwd,
                    settings=settings,
                    resolved_provider=resolved_provider,
                    session_id=session_id,
                    review=review,
                    jobs=jobs,
                )
                await store.touch(session_id)
                session_base = await load_session_totals(store, session_id)
            except Exception:
                jobs.suspended = True
                raise
            await runs.start_run(
                session_id=session_id,
                cwd=cwd,
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
            event = StreamEvent(**payload)
            yield f"data: {json.dumps(event.model_dump(exclude_none=True), ensure_ascii=False)}\n\n"

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
        active = await runs.active_run_info(session_id)
        if active:
            raise HTTPException(
                status_code=409,
                detail={"message": "session already has a running task", "run": active},
            )

        data = await store.load_session(session_id)
        if data is None:
            raise HTTPException(status_code=404, detail="session not found")
        if not has_compactable_history(data["messages"]):
            raise HTTPException(status_code=400, detail="nothing to compact")

        cwd = str(data["session"]["cwd"])
        settings = await asyncio.to_thread(get_settings, cwd)
        resolved = resolve_provider(settings, provider_name=body.provider, model=body.model)

        agent = await asyncio.to_thread(
            build_agent,
            store=store,
            cwd=cwd,
            settings=settings,
            resolved_provider=resolved,
            session_id=session_id,
        )

        try:
            run = await runs.start_compact(
                session_id=session_id,
                cwd=cwd,
                base_messages=agent.messages,
                agent=agent,
                session_base=data["totals"],
                on_complete=store.touch,
            )
        except ActiveRunError as exc:
            existing = await runs.get_run(exc.run_id)
            detail: dict[str, Any] = {"message": "session already has a running task"}
            if existing:
                detail["run"] = existing.info()
            raise HTTPException(status_code=409, detail=detail) from exc

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
        body, CliDeps.for_session(cwd=state.cwd, data_dir=store.data_dir, session_id=state.session_id)
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
        body, CliDeps.for_session(cwd=state.cwd, data_dir=store.data_dir, session_id=session_id)
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
        text=flatten_message_text(message, include_thinking=False),
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


@router.get("/config")
async def get_config(cwd: Annotated[str | None, Query()] = None) -> dict[str, Any]:
    resolved_cwd = os.path.abspath(cwd or os.getcwd())
    settings = await asyncio.to_thread(get_settings, resolved_cwd)
    skills = await asyncio.to_thread(discover_slash_skills, resolved_cwd)
    resolved: ResolvedProvider | None = None
    setup_error: dict[str, str] | None = None
    try:
        resolved = resolve_provider(settings)
    except ValueError as exc:
        setup_error = {"message": str(exc)}

    providers_info: dict[str, Any] = {}
    for provider in resolve_provider_choices(settings):
        provider_config = settings.providers.get(provider.provider_name or "")
        models = provider_models(settings, provider)

        info: dict[str, Any] = {
            "name": provider.provider_name,
            "provider": provider.provider,
            "type": provider.provider,
            "models": models,
            "base_url": provider.api_base or "",
            "has_api_key": True,
        }

        image_models: list[str] = []
        pdf_models: list[str] = []
        reasoning_efforts: dict[str, list[str]] = {}
        for model in models:
            model_config = provider_config.models.get(model) if provider_config else None
            model_meta = resolve_configured_model_metadata(
                provider=provider.provider,
                model=model,
                model_config=model_config,
            )
            efforts = list(model_meta.reasoning_efforts or ())
            reasoning_efforts[model] = efforts
            if model_meta.supports_image_input:
                image_models.append(model)
            if model_meta.supports_pdf_input:
                pdf_models.append(model)

        if provider.supports_reasoning_effort:
            info["supports_reasoning_effort"] = True
            info["reasoning_efforts"] = reasoning_efforts

        info["supports_image_input"] = bool(image_models)
        info["image_input_models"] = image_models
        info["supports_pdf_input"] = bool(pdf_models)
        info["pdf_input_models"] = pdf_models

        providers_info[provider.provider_name or provider.provider] = info

    default_payload = (
        {"provider": resolved.provider_name, "model": resolved.model}
        if resolved is not None
        else {"provider": "", "model": ""}
    )

    return {
        "providers": providers_info,
        "default": default_payload,
        "cwd": resolved_cwd,
        "cwd_exists": os.path.isdir(resolved_cwd),
        "project": settings.project,
        "config_paths": settings.config_paths,
        # Effective for this cwd; 0 disables automatic compaction.
        "compact_threshold": (
            settings.compact_threshold if settings.compact_threshold is not None else DEFAULT_COMPACT_THRESHOLD
        ),
        "skills": [{"name": skill.name, "description": skill.description} for skill in skills],
        "setup_error": setup_error,
    }
