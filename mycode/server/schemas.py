"""Pydantic models for API requests and responses."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatInputBlock(BaseModel):
    """One user input block for /chat."""

    type: Literal["text", "image"]
    text: str | None = None
    path: str | None = None
    mime_type: str | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    """Request body for /chat."""

    session_id: str = "default"
    message: str | None = None
    input: list[ChatInputBlock] | None = None
    provider: str | None = None  # provider id, or a configured provider alias
    model: str | None = None
    cwd: str | None = None
    api_key: str | None = None
    api_base: str | None = None
    reasoning_effort: str | None = None
    rewind_to: int | None = Field(default=None, description="Visible message index for rewind.")


class SessionCreateRequest(BaseModel):
    """Request body for /sessions."""

    title: str | None = None
    provider: str | None = None
    model: str | None = None
    cwd: str | None = None
    api_base: str | None = None


class ToolCallPayload(BaseModel):
    """Tool call data inside a stream event."""

    id: str
    name: str
    input: dict[str, Any]


class StreamEvent(BaseModel):
    """SSE event payload for chat streaming."""

    seq: int | None = None
    type: str
    delta: str | None = None  # text/reasoning
    tool_call: ToolCallPayload | None = None  # tool_start
    tool_use_id: str | None = None
    output: str | None = None  # tool_output
    model_text: str | None = None  # tool_done
    display_text: str | None = None  # tool_done
    is_error: bool | None = None  # tool_done
    message: str | None = None  # error
