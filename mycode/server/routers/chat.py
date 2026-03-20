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
from mycode.core.config import get_settings, provider_has_api_key, resolve_provider
from mycode.server.deps import RunManagerDep, StoreDep
from mycode.server.run_manager import ActiveRunError, RunState
from mycode.server.schemas import ChatRequest, StreamEvent

router = APIRouter()


def _format_sse(event: StreamEvent) -> str:
    return f"data: {json.dumps(event.model_dump(exclude_none=True), ensure_ascii=False)}\n\n"


def _build_agent(chat: ChatRequest):
    cwd = os.path.abspath(chat.cwd or os.getcwd())
    settings = get_settings(cwd)
    resolved = resolve_provider(
        settings,
        provider_name=chat.provider,
        model=chat.model,
        api_key=chat.api_key,
        api_base=chat.api_base,
    )
    session_id = chat.session_id or "default"
    return cwd, settings, resolved, session_id


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
    cwd, settings, resolved, session_id = _build_agent(chat)
    data = await store.get_or_create(
        session_id,
        provider=resolved.provider,
        model=resolved.model,
        cwd=cwd,
        api_base=resolved.api_base,
    )
    messages = data.get("messages") or []
    agent = Agent(
        model=resolved.model,
        provider=resolved.provider,
        cwd=cwd,
        session_dir=store.session_dir(session_id),
        api_key=resolved.api_key,
        api_base=resolved.api_base,
        messages=messages,
        settings=settings,
        reasoning_effort=resolved.reasoning_effort,
        max_tokens=resolved.max_tokens,
    )

    async def on_persist(message: dict) -> None:
        await store.append_message(session_id, message)

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

    return {"run": run}


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
    active = settings.active_provider
    try:
        default_model = resolve_provider(settings).model
    except ValueError:
        default_model = settings.default_model or (active.models[0] if active and active.models else "")
    providers_info = {
        name: {
            "name": provider.name,
            "provider": provider.type,
            "type": provider.type,
            "models": provider.models,
            "base_url": provider.base_url or "",
            "has_api_key": provider_has_api_key(provider),
        }
        for name, provider in settings.providers.items()
    }
    return {
        "providers": providers_info,
        "default": {
            "provider": settings.default_provider or "",
            "model": default_model,
        },
        "cwd": resolved_cwd,
        "workspace_root": settings.workspace_root,
        "config_paths": settings.config_paths,
    }
