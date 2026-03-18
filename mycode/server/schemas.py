"""Pydantic models for API requests and responses."""

from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str
    provider: str | None = None  # provider id, or a configured provider alias
    model: str | None = None
    cwd: str | None = None
    api_key: str | None = None
    api_base: str | None = None


class SessionCreateRequest(BaseModel):
    title: str | None = None
    provider: str | None = None
    model: str | None = None
    cwd: str | None = None
    api_base: str | None = None


class StreamEvent(BaseModel):
    """SSE event payload for chat streaming."""

    type: str
    content: str | None = None
    name: str | None = None
    args: dict | None = None
    result: str | None = None
    error: str | None = None
    message: str | None = None
    id: str | None = None
