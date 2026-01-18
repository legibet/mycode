"""Chat API endpoints."""

import json
import os
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.schemas import ChatRequest, StreamEvent
from app.session import SessionStore

router = APIRouter()
store = SessionStore()


def _set_api_key(model: str, api_key: str) -> None:
    """Set API key environment variable based on model prefix."""
    prefix_map = {
        "anthropic:": "ANTHROPIC_API_KEY",
        "openai:": "OPENAI_API_KEY",
        "gemini:": "GEMINI_API_KEY",
    }
    for prefix, env_var in prefix_map.items():
        if model.startswith(prefix):
            os.environ[env_var] = api_key
            return
    os.environ["OPENAI_API_KEY"] = api_key  # Default


def _format_sse(event: StreamEvent) -> str:
    """Format event as SSE payload."""
    return f"data: {json.dumps(event.model_dump(exclude_none=True))}\n\n"


async def _stream_chat(session_id: str, message: str, model: str, cwd: str, api_base: str | None) -> AsyncIterator[str]:
    """Stream chat events as SSE."""
    agent = await store.get_or_create(session_id, model=model, cwd=cwd, api_base=api_base)
    sent_any = False

    try:
        async for event in agent.achat(message):
            payload = StreamEvent(type=event.type, **event.data)
            yield _format_sse(payload)
            sent_any = True

        if not sent_any:
            yield _format_sse(StreamEvent(type="error", message="LLM produced no output. Check model or api_base."))
    finally:
        await store.save_session(session_id, agent)

    yield "data: [DONE]\n\n"


@router.post("/chat")
async def chat(req: ChatRequest):
    """SSE endpoint for chat."""
    settings = get_settings()
    model = req.model or settings.default_model or "anthropic:claude-sonnet-4-5"
    cwd = req.cwd or os.getcwd()
    api_base = req.api_base or settings.api_base

    if req.api_key:
        _set_api_key(model, req.api_key)

    return StreamingResponse(
        _stream_chat(req.session_id, req.message, model, cwd, api_base),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/cancel")
async def cancel(session_id: str = "default"):
    """Cancel running tool processes for session."""
    agent = store.get(session_id)
    if agent:
        agent.cancel()
    return {"status": "ok"}


@router.get("/config")
async def get_config():
    """Get current config."""
    settings = get_settings()
    return {
        "model": settings.default_model or "",
        "api_base": settings.api_base or "",
        "cwd": os.getcwd(),
    }
