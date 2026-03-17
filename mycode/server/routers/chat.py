"""Chat API (SSE streaming)."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from mycode.core.agent import Agent
from mycode.core.config import get_settings, resolve_provider
from mycode.core.tools import cancel_all_tools
from mycode.server.deps import store
from mycode.server.schemas import ChatRequest, StreamEvent

router = APIRouter()


def _format_sse(event: StreamEvent) -> str:
    return f"data: {json.dumps(event.model_dump(exclude_none=True), ensure_ascii=False)}\n\n"


async def _stream_chat(req: Request, chat: ChatRequest) -> AsyncIterator[str]:
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

    data = await store.get_or_create(session_id, model=resolved.model, cwd=cwd, api_base=resolved.api_base)
    messages = data.get("messages") or []
    session_dir = store.session_dir(session_id)

    agent = Agent(
        model=resolved.model,
        provider=resolved.provider_type,
        cwd=cwd,
        session_dir=session_dir,
        api_key=resolved.api_key,
        api_base=resolved.api_base,
        messages=messages,
        settings=settings,
    )

    async def on_persist(message: dict) -> None:
        await store.append_message(session_id, message)

    sent_any = False

    try:
        async for ev in agent.achat(chat.message, on_persist=on_persist):
            if await req.is_disconnected():
                agent.cancel()
                break

            payload = StreamEvent(type=ev.type, **ev.data)
            yield _format_sse(payload)
            sent_any = True

        if not sent_any:
            yield _format_sse(StreamEvent(type="error", message="LLM produced no output."))

    except Exception as exc:
        yield _format_sse(StreamEvent(type="error", message=str(exc)))

    yield "data: [DONE]\n\n"


@router.post("/chat")
async def chat(req: Request, chat: ChatRequest):
    return StreamingResponse(
        _stream_chat(req, chat),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/cancel")
async def cancel(session_id: str = "default"):
    """Best-effort cancellation."""
    cancel_all_tools()
    return {"status": "ok", "session_id": session_id}


@router.get("/config")
async def get_config(cwd: str | None = None):
    resolved_cwd = os.path.abspath(cwd or os.getcwd())
    settings = get_settings(resolved_cwd)
    active = settings.active_provider
    default_model = settings.default_model or (active.models[0] if active and active.models else "")
    providers_info = {
        name: {
            "name": p.name,
            "type": p.type,
            "models": p.models,
            "base_url": p.base_url or "",
            "has_api_key": bool(p.api_key),
        }
        for name, p in settings.providers.items()
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
