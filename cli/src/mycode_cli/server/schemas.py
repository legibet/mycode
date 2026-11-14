"""Pydantic models for API requests and responses."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from mycode.messages import CompactTrigger
from mycode.usage import Cost
from mycode_cli.sessions import ID_PATTERN, SessionInfo

# ``agent``: one turn of a subagent, keyed ``<parent id>/<child id>``.
RunKind = Literal["chat", "compact", "agent"]
RunStatus = Literal["running", "completed", "failed", "cancelled"]


class ChatInputBlock(BaseModel):
    """One user input block for /chat."""

    type: Literal["text", "image", "document"]
    text: str | None = None
    path: str | None = None
    data: str | None = None  # inline base64 (web upload)
    name: str | None = None
    is_attachment: bool = False

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.type == "text":
            # A text block is either inline text or a server-side file path
            # (attachment), never both, and a path must be an attachment.
            if self.path is not None:
                if self.text is not None:
                    raise ValueError("text block: text and path are mutually exclusive")
                if not self.is_attachment:
                    raise ValueError("text block with path requires is_attachment")
            return self

        if not self.path and not self.data:
            raise ValueError(f"{self.type} input requires path or data")
        return self


class UserInputRequest(BaseModel):
    """User message fields shared by /chat, steer, and queue requests."""

    message: str | None = None
    input: list[ChatInputBlock] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        has_message = bool((self.message or "").strip())
        has_input = bool(self.input)
        if has_message and has_input:
            raise ValueError("message and input are mutually exclusive")
        if not has_message and not has_input:
            raise ValueError("message or input is required")
        return self


class ChatRequest(UserInputRequest):
    """Request body for /chat."""

    # Validated like the session routes' path parameter: it names a directory.
    session_id: str = Field(default="default", pattern=ID_PATTERN)
    provider: str | None = None  # provider id, or a configured provider alias
    model: str | None = None
    cwd: str | None = None
    reasoning_effort: str | None = None
    rewind_to: int | None = Field(default=None, description="Visible message index for rewind.")


class PendingInputRequest(UserInputRequest):
    """Request body for /runs/{run_id}/steer and /sessions/{session_id}/queue."""

    input_id: str = Field(min_length=1, description="Client-generated id, stored as meta.input_id.")


class SessionCreateRequest(BaseModel):
    """Request body for /sessions."""

    cwd: str | None = None


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
    duration_ms: int | None = None  # reasoning_done
    tool_call: ToolCallPayload | None = None  # tool_start
    tool_use_id: str | None = None
    output: str | None = None  # tool_output + tool_done
    metadata: dict[str, Any] | None = None  # tool_output + tool_done
    is_error: bool | None = None  # tool_done
    message: str | dict[str, Any] | None = None  # error text, or the user_message record
    request_id: str | None = None  # permission_request + permission_resolved
    tool_name: str | None = None  # permission_request
    preview: str | None = None  # permission_request
    agent: str | None = None  # permission_request: the description of the subagent asking
    decision: str | None = None  # permission_resolved
    trigger: CompactTrigger | None = None  # compact
    context_tokens: int | None = None  # usage
    context_window: int | None = None  # usage
    model: str | None = None  # usage
    turn_usage: dict[str, int] | None = None  # usage
    turn_cost: Cost | None = None  # usage
    turn_duration_ms: int | None = None  # usage
    session_usage: dict[str, int] | None = None  # usage; composed by the run manager
    session_cost: Cost | None = None  # usage; composed by the run manager


class AgentSessionInfo(BaseModel):
    """The ``session`` of a subagent snapshot, read from the child's ``agent.json``."""

    id: str
    agent: str
    description: str
    provider: str
    model: str
    created_at: str
    # The last finished turn; absent while the first turn runs and after a crash.
    last: dict[str, Any] | None = None


class DecideRequest(BaseModel):
    """Request body for /runs/{run_id}/decide."""

    request_id: str
    decision: Literal["allow", "deny"]


class SettingsRequest(BaseModel):
    """Request body for PUT /settings — full replacement of global config."""

    config: dict[str, Any] | None = None


class RunInfo(BaseModel):
    """Public run state returned by the server."""

    id: str
    session_id: str
    kind: RunKind
    status: RunStatus
    last_seq: int
    error: str | None = None


class CompactRequest(BaseModel):
    """Request body for POST /sessions/{session_id}/compact."""

    provider: str | None = None  # provider id, or a configured provider alias
    model: str | None = None
    reasoning_effort: str | None = None


class ChatResponse(BaseModel):
    """Response for POST /chat."""

    run: RunInfo
    session: SessionInfo
    # The user message as the run received it: finished background results
    # that waited for this message ride ahead of its own blocks.
    message: dict[str, Any]


class RunResponse(BaseModel):
    """Response carrying one run: compact start, steer, queue, and queue removal."""

    run: RunInfo


class StatusResponse(BaseModel):
    """Simple status response."""

    status: Literal["ok"]


class CancelRunResponse(StatusResponse):
    """Response for POST /runs/{run_id}/cancel."""

    run: RunInfo
