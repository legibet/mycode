"""Chat API (SSE streaming)."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.agent.core import Agent, Event
from app.agent.tools import cancel_all_tools
from app.config import get_settings
from app.schemas import ChatRequest, StreamEvent
from app.session import SessionStore

router = APIRouter()
store = SessionStore()


def _format_sse(event: StreamEvent) -> str:
    return f"data: {json.dumps(event.model_dump(exclude_none=True), ensure_ascii=False)}\n\n"


async def _stream_chat(req: Request, chat: ChatRequest) -> AsyncIterator[str]:
    settings = get_settings()

    model = chat.model or settings.default_model or "anthropic:claude-sonnet-4-5"
    cwd = os.path.abspath(chat.cwd or os.getcwd())
    api_base = chat.api_base or settings.api_base
    api_key = chat.api_key

    session_id = chat.session_id or "default"

    data = await store.get_or_create(session_id, model=model, cwd=cwd, api_base=api_base)
    messages = data.get("messages") or []

    session_dir = store.session_dir(session_id)

    agent = Agent(
        model=model,
        cwd=cwd,
        session_dir=session_dir,
        api_key=api_key,
        api_base=api_base,
        messages=messages,
    )

    async def on_persist(message: dict) -> None:
        # Persist only non-system messages (Agent never calls on_persist for system messages).
        await store.append_message(session_id, message)

    sent_any = False

    try:
        async for ev in agent.achat(chat.message, on_persist=on_persist):
            # Stop if client disconnected
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
    """Best-effort cancellation.

    - Kills running bash subprocesses.
    - Does not cancel in-flight LLM streaming (provider-dependent).
    """

    cancel_all_tools()
    return {"status": "ok", "session_id": session_id}


@router.get("/config")
async def get_config():
    settings = get_settings()
    return {"model": settings.default_model or "", "api_base": settings.api_base or "", "cwd": os.getcwd()}
