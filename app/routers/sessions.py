"""Session management API endpoints."""

import os

from fastapi import APIRouter

from app.config import get_settings
from app.schemas import SessionCreateRequest
from app.session import SessionStore

router = APIRouter(prefix="/sessions", tags=["sessions"])
store = SessionStore()


@router.post("")
async def create_session(req: SessionCreateRequest):
    """Create a new chat session."""
    settings = get_settings()
    model = req.model or settings.default_model or "anthropic:claude-sonnet-4-5"
    cwd = req.cwd or os.getcwd()
    api_base = req.api_base or settings.api_base
    return await store.create_session(req.title, model=model, cwd=cwd, api_base=api_base)


@router.get("")
async def list_sessions(cwd: str | None = None):
    """List chat sessions."""
    return {"sessions": await store.list_sessions(cwd=cwd)}


@router.get("/{session_id}")
async def load_session(session_id: str):
    """Load a chat session."""
    data = await store.load_session(session_id)
    if not data:
        return {"session": None, "messages": []}
    return data


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    """Delete a chat session."""
    await store.delete(session_id)
    return {"status": "ok"}


@router.post("/{session_id}/clear")
async def clear_session(session_id: str):
    """Clear conversation history."""
    await store.clear(session_id)
    return {"status": "ok"}
