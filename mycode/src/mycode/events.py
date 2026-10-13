"""Streaming events yielded by :meth:`mycode.Agent.achat`.

Each event is a frozen dataclass whose ``type`` names it on the wire;
:data:`Event` is their union. Match on the class, or compare ``event.type``.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, ClassVar, Literal

from mycode.messages import CompactTrigger, ConversationMessage, ImageBlock, TextBlock, ToolUseBlock
from mycode.providers.base import ProviderErrorReason
from mycode.usage import Cost, Usage


@dataclass(frozen=True, slots=True, kw_only=True)
class _BaseEvent:
    type: ClassVar[str]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready payload: ``type`` plus the fields.

        Optional fields (those defaulting to ``None``) are left out while
        unset; other fields are always present, even when ``None``. Values are
        not copied.
        """

        data: dict[str, Any] = {"type": self.type}
        for field in fields(self):
            value = getattr(self, field.name)
            if value is not None or field.default is not None:
                data[field.name] = value
        return data


@dataclass(frozen=True, slots=True, kw_only=True)
class TextEvent(_BaseEvent):
    """A chunk of response text."""

    type: ClassVar[Literal["text"]] = "text"
    delta: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ReasoningEvent(_BaseEvent):
    """A chunk of reasoning text."""

    type: ClassVar[Literal["reasoning"]] = "reasoning"
    delta: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ReasoningDoneEvent(_BaseEvent):
    """Reasoning for the current response ended after ``duration_ms``."""

    type: ClassVar[Literal["reasoning_done"]] = "reasoning_done"
    duration_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolStartEvent(_BaseEvent):
    """A tool call is about to run, before its hooks."""

    type: ClassVar[Literal["tool_start"]] = "tool_start"
    tool_call: ToolUseBlock


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolOutputEvent(_BaseEvent):
    """Live display output from a streaming tool."""

    type: ClassVar[Literal["tool_output"]] = "tool_output"
    tool_use_id: str
    output: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolDoneEvent(_BaseEvent):
    """A tool call's final result, as persisted in its ``tool_result`` block."""

    type: ClassVar[Literal["tool_done"]] = "tool_done"
    tool_use_id: str
    output: str
    is_error: bool
    metadata: dict[str, Any] | None = None
    content: list[TextBlock | ImageBlock] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageEvent(_BaseEvent):
    """Context occupancy plus the current turn segment's usage, cost, and elapsed time.

    ``None`` means the value is unknown; see docs/sdk.md.
    """

    type: ClassVar[Literal["usage"]] = "usage"
    context_tokens: int | None
    turn_usage: Usage
    turn_cost: Cost | None
    turn_duration_ms: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactEvent(_BaseEvent):
    """A compact marker was committed."""

    type: ClassVar[Literal["compact"]] = "compact"
    trigger: CompactTrigger


@dataclass(frozen=True, slots=True, kw_only=True)
class UserMessageEvent(_BaseEvent):
    """Merged steers were committed as one user message and delivered."""

    type: ClassVar[Literal["user_message"]] = "user_message"
    message: ConversationMessage


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryEvent(_BaseEvent):
    """A failed provider request will be retried after ``delay_seconds``."""

    type: ClassVar[Literal["retry"]] = "retry"
    attempt: int
    max_attempts: int
    delay_seconds: float
    reason: ProviderErrorReason
    message: str
    status_code: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorEvent(_BaseEvent):
    """The turn ended with an error."""

    type: ClassVar[Literal["error"]] = "error"
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelledEvent(_BaseEvent):
    """The turn ended after a user stop and its cleanup."""

    type: ClassVar[Literal["cancelled"]] = "cancelled"


type Event = (
    TextEvent
    | ReasoningEvent
    | ReasoningDoneEvent
    | ToolStartEvent
    | ToolOutputEvent
    | ToolDoneEvent
    | UsageEvent
    | CompactEvent
    | UserMessageEvent
    | RetryEvent
    | ErrorEvent
    | CancelledEvent
)
