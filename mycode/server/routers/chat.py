"""Chat and run streaming API."""

from __future__ import annotations

import asyncio
import json
import os
from base64 import b64encode
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from mycode.core.agent import Agent
from mycode.core.config import (
    get_settings,
    normalize_reasoning_effort,
    resolve_provider,
    resolve_provider_choices,
)
from mycode.core.messages import (
    ConversationMessage,
    build_message,
    document_block,
    flatten_message_text,
    image_block,
    text_block,
)
from mycode.core.providers import get_provider_adapter, provider_default_models
from mycode.core.tools import detect_document_mime_type, detect_image_mime_type, resolve_path
from mycode.server.deps import RunManagerDep, StoreDep
from mycode.server.run_manager import ActiveRunError, RunState
from mycode.server.schemas import ChatRequest, StreamEvent

router = APIRouter()

REASONING_EFFORT_OPTIONS = ("auto", "none", "low", "medium", "high", "xhigh")


def _format_sse(event: StreamEvent) -> str:
    return f"data: {json.dumps(event.model_dump(exclude_none=True), ensure_ascii=False)}\n\n"


async def _stream_run(req: Request, state: RunState, after: int) -> AsyncIterator[str]:
    last_seq = max(0, after)

    while True:
        if await req.is_disconnected():
            return

        async with state.condition:
            pending = [event for event in state.events if int(event.get("seq") or 0) > last_seq]
            finished = state.status != "running"

            if not pending and not finished:
                try:
                    await asyncio.wait_for(state.condition.wait(), timeout=0.5)
                except TimeoutError:
                    continue
                continue

        for payload in pending:
            if await req.is_disconnected():
                return
            yield _format_sse(StreamEvent(**payload))
            last_seq = int(payload.get("seq") or last_seq)

        if finished:
            break

    yield "data: [DONE]\n\n"


@router.post("/chat")
async def chat(chat: ChatRequest, store: StoreDep, runs: RunManagerDep):
    cwd = os.path.abspath(chat.cwd or os.getcwd())
    settings = get_settings(cwd)
    resolved = resolve_provider(
        settings,
        provider_name=chat.provider,
        model=chat.model,
        api_key=chat.api_key,
        api_base=chat.api_base,
    )
    request_effort = normalize_reasoning_effort(chat.reasoning_effort)
    reasoning_effort = request_effort if request_effort is not None else resolved.reasoning_effort
    session_id = chat.session_id or "default"

    if chat.message and chat.input:
        raise HTTPException(status_code=400, detail="message and input are mutually exclusive")

    if chat.input:
        blocks: list[dict[str, Any]] = []
        for block in chat.input:
            if block.type == "text":
                text = str(block.text or "")
                if text:
                    blocks.append(text_block(text))
                continue

            if block.type == "document":
                if block.data:
                    mime_type = block.mime_type or "application/pdf"
                    if mime_type != "application/pdf":
                        raise HTTPException(status_code=400, detail="unsupported document mime_type")
                    blocks.append(document_block(block.data, mime_type=mime_type, name=block.name or "document.pdf"))
                    continue

                if not block.path:
                    raise HTTPException(status_code=400, detail="document input requires path or data")
                resolved_path = resolve_path(block.path, cwd=cwd)
                document_path = Path(resolved_path)
                if not document_path.exists() or not document_path.is_file():
                    raise HTTPException(status_code=400, detail=f"document file not found: {block.path}")
                mime_type = block.mime_type or detect_document_mime_type(document_path)
                if mime_type != "application/pdf":
                    raise HTTPException(status_code=400, detail=f"unsupported document file: {block.path}")
                document_data = b64encode(document_path.read_bytes()).decode("utf-8")
                blocks.append(
                    document_block(
                        document_data,
                        mime_type=mime_type,
                        name=block.name or document_path.name,
                    )
                )
                continue

            if block.data:
                # Inline base64 from web upload
                if not block.mime_type:
                    raise HTTPException(status_code=400, detail="image data requires mime_type")
                blocks.append(image_block(block.data, mime_type=block.mime_type, name=block.name or "image"))
                continue

            if not block.path:
                raise HTTPException(status_code=400, detail="image input requires path or data")
            resolved_path = resolve_path(block.path, cwd=cwd)
            image_path = Path(resolved_path)
            if not image_path.exists() or not image_path.is_file():
                raise HTTPException(status_code=400, detail=f"image file not found: {block.path}")
            mime_type = block.mime_type or detect_image_mime_type(image_path)
            if not mime_type:
                raise HTTPException(status_code=400, detail=f"unsupported image file: {block.path}")
            image_data = b64encode(image_path.read_bytes()).decode("utf-8")
            blocks.append(image_block(image_data, mime_type=mime_type, name=block.name or image_path.name))

        if not blocks:
            raise HTTPException(status_code=400, detail="input must include at least one non-empty block")
        user_message = build_message("user", blocks)
    else:
        message_text = str(chat.message or "").strip()
        if not message_text:
            raise HTTPException(status_code=400, detail="message or input is required")
        user_message = build_message("user", [text_block(message_text)])

    if any(isinstance(block, dict) and block.get("type") == "image" for block in user_message.get("content") or []):
        if resolved.supports_image_input is not True:
            raise HTTPException(status_code=400, detail="current model does not support image input")
    if any(isinstance(block, dict) and block.get("type") == "document" for block in user_message.get("content") or []):
        if resolved.supports_pdf_input is not True:
            raise HTTPException(status_code=400, detail="current model does not support PDF input")

    data = await store.load_session(session_id)
    session = (data or {}).get("session")
    messages = (data or {}).get("messages") or []

    if not session and chat.rewind_to is not None:
        raise HTTPException(status_code=400, detail="rewind_to requires an existing session")

    if not session:
        title = flatten_message_text(user_message).replace("\n", " ").strip()[:48] or "New chat"
        created = await store.create_session(
            title,
            session_id=session_id,
            provider=resolved.provider,
            model=resolved.model,
            cwd=cwd,
            api_base=resolved.api_base,
        )
        session = created["session"]

    if chat.rewind_to is not None:
        if not (0 <= chat.rewind_to < len(messages)):
            raise HTTPException(
                status_code=400,
                detail=f"rewind_to must reference a visible message index between 0 and {len(messages) - 1}",
            )

        target = messages[chat.rewind_to]
        raw_blocks = target.get("content")
        blocks = raw_blocks if isinstance(raw_blocks, list) else []
        has_user_content = any(
            (block.get("type") == "text" and block.get("text")) or block.get("type") in {"image", "document"}
            for block in blocks
        )

        # Rewind only makes sense for real user prompts. Synthetic compact
        # summaries, assistant messages, and tool-result-only user messages are
        # not valid targets.
        if target.get("role") != "user" or (target.get("meta") or {}).get("synthetic") or not has_user_content:
            raise HTTPException(
                status_code=400,
                detail="rewind_to must reference a real user message",
            )

        messages = messages[: chat.rewind_to]

    agent = Agent(
        model=resolved.model,
        provider=resolved.provider,
        cwd=cwd,
        session_dir=store.session_dir(session_id),
        session_id=session_id,
        api_key=resolved.api_key,
        api_base=resolved.api_base,
        messages=messages,
        settings=settings,
        reasoning_effort=reasoning_effort,
        supports_image_input=resolved.supports_image_input,
        supports_pdf_input=resolved.supports_pdf_input,
        max_tokens=resolved.max_tokens,
        context_window=resolved.context_window,
        compact_threshold=settings.compact_threshold,
    )

    rewind_persisted = False

    async def on_persist(message: ConversationMessage) -> None:
        nonlocal rewind_persisted
        if chat.rewind_to is not None and not rewind_persisted:
            await store.append_rewind(session_id, chat.rewind_to)
            rewind_persisted = True
        await store.append_message(
            session_id,
            message,
            provider=resolved.provider,
            model=resolved.model,
            cwd=cwd,
            api_base=resolved.api_base,
        )

    try:
        run = await runs.start_run(
            session_id=session_id,
            user_message=user_message,
            base_messages=messages,
            agent=agent,
            on_persist=on_persist,
        )
    except ActiveRunError as exc:
        existing = await runs.get_run(exc.run_id)
        detail: dict[str, Any] = {"message": "session already has a running task"}
        if existing:
            detail["run"] = existing.info()
        raise HTTPException(status_code=409, detail=detail) from exc

    return {"run": run, "session": session}


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, req: Request, runs: RunManagerDep, after: int = 0):
    state = await runs.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")

    return StreamingResponse(
        _stream_run(req, state, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, runs: RunManagerDep):
    run = await runs.cancel_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return {"status": "ok", "run": run}


@router.get("/config")
async def get_config(cwd: str | None = None):
    resolved_cwd = os.path.abspath(cwd or os.getcwd())
    settings = get_settings(resolved_cwd)
    try:
        resolved = resolve_provider(settings)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

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
            resolved_model = resolve_provider(
                settings,
                provider_name=provider.provider_name or provider.provider,
                model=model,
                api_base=provider.api_base or None,
            )
            if resolved_model.supports_reasoning is True:
                reasoning_models.append(model)
            if resolved_model.supports_image_input is True:
                image_models.append(model)
            if resolved_model.supports_pdf_input is True:
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

    return {
        "providers": providers_info,
        "default": {
            "provider": resolved.provider_name,
            "model": resolved.model,
        },
        "default_reasoning_effort": settings.default_reasoning_effort,
        "reasoning_effort_options": REASONING_EFFORT_OPTIONS,
        "cwd": resolved_cwd,
        "workspace_root": settings.workspace_root,
        "config_paths": settings.config_paths,
    }
