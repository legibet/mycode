"""Pydantic models for API requests and responses."""

from typing import Any

from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str
    provider: str | None = None  # provider id, or a configured provider alias
    model: str | None = None
    cwd: str | None = None
    api_key: str | None = None
    api_base: str | None = None
    reasoning_effort: str | None = None
    rewind_to: int | None = None  # truncate session to this message index before sending


class SessionCreateRequest(BaseModel):
    title: str | None = None
    provider: str | None = None
    model: str | None = None
    cwd: str | None = None
    api_base: str | None = None


class ToolCallPayload(BaseModel):
    id: str
    name: str
    input: dict[str, Any]


class StreamEvent(BaseModel):
    """SSE event payload for chat streaming."""

    seq: int | None = None
    type: str
    delta: str | None = None
    tool_call: ToolCallPayload | None = None
    tool_use_id: str | None = None
    output: str | None = None
    model_text: str | None = None
    display_text: str | None = None
    is_error: bool | None = None
    message: str | None = None
