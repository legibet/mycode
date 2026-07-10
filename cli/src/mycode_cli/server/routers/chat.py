"""Chat and run streaming API."""

from __future__ import annotations

import asyncio
import json
import os
from base64 import b64encode
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse

from mycode.attachments import (
    Attachment,
    build_attachment_blocks,
    detect_document_mime_type,
    detect_image_mime_type,
)
from mycode.messages import (
    ConversationMessage,
    build_message,
    document_block,
    image_block,
    text_block,
)
from mycode.models import resolve_model_metadata
from mycode.providers import get_provider_adapter, provider_default_models
from mycode.utils import resolve_path
from mycode_cli.config import (
    REASONING_EFFORT_OPTIONS,
    ResolvedProvider,
    gate_reasoning_effort,
    get_settings,
    normalize_reasoning_effort,
    resolve_provider,
    resolve_provider_choices,
)
from mycode_cli.permissions import ToolReviewDecision, ToolReviewRequest
from mycode_cli.runtime import build_agent
from mycode_cli.server.deps import RunManagerDep, StoreDep, resolve_workspace_cwd
from mycode_cli.server.run_manager import ActiveRunError
from mycode_cli.server.schemas import (
    CancelRunResponse,
    ChatRequest,
    ChatResponse,
    DecideRequest,
    RunInfo,
    StatusResponse,
    StreamEvent,
)

router = APIRouter()


async def _build_user_message(chat: ChatRequest, cwd: str) -> ConversationMessage:
    if not chat.input:
        return build_message("user", [text_block(str(chat.message or "").strip())])

    blocks: list[dict[str, Any]] = []
    for block in chat.input:
        if block.type == "text":
            text = block.text or ""
            if block.is_attachment:
                blocks.extend(
                    build_attachment_blocks(
                        [Attachment.text(text, name=str(block.name or "attached-file"))],
                        cwd=cwd,
                    )
                )
            elif text:
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
        path = Path(resolve_path(cast(str, block.path), cwd=cwd))
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
    return build_message("user", blocks)


def _validate_rewind_request(
    *,
    session: dict[str, Any] | None,
    messages: list[ConversationMessage],
    rewind_to: int | None,
) -> None:
    if rewind_to is None:
        return

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
        and ((block.get("type") == "text" and block.get("text")) or block.get("type") in {"image", "document"})
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
    try:
        request_effort = normalize_reasoning_effort(chat.reasoning_effort)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session_id = chat.session_id or "default"
    user_message = await _build_user_message(chat, cwd)

    # Capability check before any disk mutation — a failed check must not
    # leave an empty session on disk or land a premature rewind marker.
    model_config = resolved.model_config
    model_meta = resolve_model_metadata(
        provider=resolved.provider,
        model=resolved.model,
        supports_reasoning=model_config.supports_reasoning if model_config else None,
        supports_image_input=model_config.supports_image_input if model_config else None,
        supports_pdf_input=model_config.supports_pdf_input if model_config else None,
    )
    content_types = {b.get("type") for b in (user_message.get("content") or []) if isinstance(b, dict)}
    if "image" in content_types and not model_meta.supports_image_input:
        raise HTTPException(status_code=400, detail="current model does not support image input")
    if "document" in content_types and not model_meta.supports_pdf_input:
        raise HTTPException(status_code=400, detail="current model does not support PDF input")

    adapter = get_provider_adapter(resolved.provider)
    configured_effort = request_effort if request_effort is not None else resolved.reasoning_effort
    reasoning_effort = gate_reasoning_effort(
        configured_effort,
        supports_reasoning=model_meta.supports_reasoning,
        adapter_supports_effort=adapter.supports_reasoning_effort,
    )

    async with runs.session_operation(session_id):
        active = await runs.active_run_info(session_id)
        if active:
            raise HTTPException(
                status_code=409,
                detail={"message": "session already has a running task", "run": active},
            )

        data = await store.load_session(session_id)
        session = cast(dict[str, Any] | None, data["session"] if data else None)
        existing_messages = data["messages"] if data else []
        _validate_rewind_request(session=session, messages=existing_messages, rewind_to=chat.rewind_to)

        # All validation passed. Land the rewind marker (if any) and create the
        # session so the response payload has a meta dict and the agent's
        # auto-resume sees the correct on-disk state.
        if chat.rewind_to is not None:
            await store.append_rewind(session_id, chat.rewind_to)
        if not session:
            created = await store.create_session(session_id, cwd=cwd)
            session = created["session"]

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
            resolved_provider=resolved,
            session_id=session_id,
            reasoning_effort=reasoning_effort,
            review=review,
        )

        try:
            run = await runs.start_run(
                session_id=session_id,
                user_message=user_message,
                base_messages=agent.messages,
                agent=agent,
            )
        except ActiveRunError as exc:
            existing = await runs.get_run(exc.run_id)
            detail: dict[str, Any] = {"message": "session already has a running task"}
            if existing:
                detail["run"] = existing.info()
            raise HTTPException(status_code=409, detail=detail) from exc

    return ChatResponse(run=RunInfo.model_validate(run), session=session)


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


@router.get("/config")
async def get_config(cwd: Annotated[str | None, Query()] = None) -> dict[str, Any]:
    resolved_cwd = os.path.abspath(cwd or os.getcwd())
    settings = await asyncio.to_thread(get_settings, resolved_cwd)
    resolved: ResolvedProvider | None = None
    setup_error: dict[str, str] | None = None
    try:
        resolved = resolve_provider(settings)
    except ValueError as exc:
        setup_error = {"message": str(exc)}

    providers_info: dict[str, Any] = {}
    for provider in resolve_provider_choices(settings):
        provider_config = settings.providers.get(provider.provider_name or "")
        models = (
            list(provider_config.models)
            if provider_config and provider_config.models
            else list(provider_default_models(provider.provider))
        )
        if not models:
            models = [provider.model]

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
        reasoning_models: list[str] = []
        adapter = get_provider_adapter(provider.provider)
        for model in models:
            model_config = provider_config.models.get(model) if provider_config else None
            model_meta = resolve_model_metadata(
                provider=provider.provider,
                model=model,
                supports_reasoning=model_config.supports_reasoning if model_config else None,
                supports_image_input=model_config.supports_image_input if model_config else None,
                supports_pdf_input=model_config.supports_pdf_input if model_config else None,
            )
            if model_meta.supports_reasoning is True:
                reasoning_models.append(model)
            if model_meta.supports_image_input:
                image_models.append(model)
            if model_meta.supports_pdf_input:
                pdf_models.append(model)

        if adapter.supports_reasoning_effort:
            info["supports_reasoning_effort"] = True
            info["reasoning_models"] = reasoning_models
            info["reasoning_effort"] = provider.reasoning_effort

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
        "default_reasoning_effort": settings.default_reasoning_effort,
        "reasoning_effort_options": REASONING_EFFORT_OPTIONS,
        "cwd": resolved_cwd,
        "cwd_exists": os.path.isdir(resolved_cwd),
        "project": settings.project,
        "config_paths": settings.config_paths,
        "setup_error": setup_error,
    }
