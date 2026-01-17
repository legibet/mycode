import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.schemas.chat import ChatRequest
from app.services.session_service import SessionStore
from app.services.stream_service import stream_events

router = APIRouter()
store = SessionStore()


def set_api_key(model: str, api_key: str) -> None:
    """Set API key based on model prefix."""
    if model.startswith("anthropic:"):
        os.environ["ANTHROPIC_API_KEY"] = api_key
    elif model.startswith("openai:"):
        os.environ["OPENAI_API_KEY"] = api_key
    elif model.startswith("gemini:"):
        os.environ["GEMINI_API_KEY"] = api_key
    else:
        os.environ["OPENAI_API_KEY"] = api_key


@router.post("/chat")
async def chat(req: ChatRequest):
    """SSE endpoint for chat."""
    settings = get_settings()
    model = req.model or settings.default_model or "anthropic:claude-sonnet-4-5"
    cwd = req.cwd or os.getcwd()
    api_base = req.api_base or settings.api_base

    if req.api_key:
        set_api_key(model, req.api_key)

    agent = store.get_or_create(req.session_id, model=model, cwd=cwd, api_base=api_base)

    return StreamingResponse(
        stream_events(agent, req.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/clear")
async def clear(session_id: str = "default"):
    """Clear conversation history."""
    store.clear(session_id)
    return {"status": "ok"}


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


@router.get("/cwd")
async def list_cwd():
    """List current directory for UI validation."""
    return {
        "cwd": os.getcwd(),
        "exists": Path(os.getcwd()).exists(),
    }
