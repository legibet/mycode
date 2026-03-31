"""Chat and run streaming API."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
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
from mycode.core.models import lookup_model_metadata
from mycode.core.providers import get_provider_adapter, provider_default_models
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

    data = await store.load_session(session_id)
    session = (data or {}).get("session")
    messages = (data or {}).get("messages") or []

    if not session and chat.rewind_to is not None:
        raise HTTPException(status_code=400, detail="rewind_to requires an existing session")

    if not session:
        title = chat.message.replace("\n", " ").strip()[:48] or "New chat"
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
        blocks = target.get("content")
        has_user_text = isinstance(blocks, list) and any(
            isinstance(block, dict) and block.get("type") == "text" and block.get("text") for block in blocks
        )

        # Rewind only makes sense for real user prompts. Synthetic compact
        # summaries, assistant messages, and tool-result-only user messages are
        # not valid targets.
        if target.get("role") != "user" or (target.get("meta") or {}).get("synthetic") or not has_user_text:
            raise HTTPException(
                status_code=400,
                detail="rewind_to must reference a real user text message",
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
        max_tokens=resolved.max_tokens,
        context_window=resolved.context_window,
        compact_threshold=settings.compact_threshold,
    )

    rewind_persisted = False

    async def on_persist(message: dict) -> None:
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
            user_input=chat.message,
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

        adapter = get_provider_adapter(provider.provider)
        if adapter.supports_reasoning_effort:
            reasoning_models: list[str] = []
            for model in models:
                meta = lookup_model_metadata(
                    provider_type=provider.provider,
                    provider_name=provider.provider_name or provider.provider,
                    model=model,
                    api_base=provider.api_base or None,
                )
                if meta and meta.supports_reasoning is True:
                    reasoning_models.append(model)

            info["supports_reasoning_effort"] = True
            info["reasoning_models"] = reasoning_models
            info["reasoning_effort"] = provider.reasoning_effort

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
