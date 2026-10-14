"""Internal conversation model shared by the runtime, session store, CLI, and UI.

Provider adapters translate this shape to and from provider-specific wire
formats so the agent loop and session store stay provider-agnostic.

Metadata layout:

- ``meta.created_at`` (ISO-8601 UTC) marks when the agent committed the
  message to the timeline
- assistant message ``meta`` keeps only normalized top-level fields:
  ``provider``, ``model``, ``provider_message_id``, ``stop_reason``,
  ``usage``, ``cost``, ``context_window`` (see docs/sessions.md for ``usage``
  semantics); ``model`` records the selected request model
- provider-specific extras live under ``meta.native`` on messages and
  ``block.meta.native`` on blocks
- local display metadata such as ``block.meta.duration_ms`` is never sent
  upstream
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, NotRequired

from typing_extensions import TypedDict

from mycode.usage import Cost, Usage

type StopReason = Literal["stop", "tool_use", "length", "error", "cancelled", "unknown"]
# What started a compaction: the agent loop crossing the threshold inside a
# turn, or an explicit acompact() call. Their markers sit in the same places in
# the log, so the origin is recorded instead of inferred.
type CompactTrigger = Literal["auto", "manual"]


class BlockMeta(TypedDict, total=False, extra_items=Any):
    """Block metadata; applications may add their own keys."""

    # Provider-specific replay data.
    native: dict[str, Any]
    # Thinking duration, for display only.
    duration_ms: int
    # A text block expanded from a user attachment; ``path`` labels it in UIs.
    attachment: bool
    path: str
    # A tool call whose streamed arguments were not valid JSON.
    invalid_input: bool
    parse_error: str
    raw_arguments: str


class MessageMeta(TypedDict, total=False, extra_items=Any):
    """Message metadata; applications may add their own keys."""

    created_at: str
    # Assistant responses and compact markers.
    provider: str
    model: str
    provider_message_id: str
    stop_reason: StopReason
    error: str
    usage: Usage
    cost: Cost
    context_window: int
    native: dict[str, Any]
    # User input and merged steers.
    input_id: str
    input_ids: list[str]
    steer: bool
    # Compact markers.
    trigger: CompactTrigger


class TextBlock(TypedDict):
    type: Literal["text"]
    text: str
    meta: NotRequired[BlockMeta]


class ThinkingBlock(TypedDict):
    type: Literal["thinking"]
    text: str
    meta: NotRequired[BlockMeta]


class ImageBlock(TypedDict):
    type: Literal["image"]
    data: str
    mime_type: str
    name: NotRequired[str]
    meta: NotRequired[BlockMeta]


class DocumentBlock(TypedDict):
    type: Literal["document"]
    data: str
    mime_type: str
    name: NotRequired[str]
    meta: NotRequired[BlockMeta]


class ToolUseBlock(TypedDict):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]
    meta: NotRequired[BlockMeta]


class ToolResultBlock(TypedDict):
    type: Literal["tool_result"]
    tool_use_id: str
    # Replayed to providers on later turns.
    output: str
    is_error: bool
    # Multimodal replay content, e.g. an image returned by a tool.
    content: NotRequired[list[TextBlock | ImageBlock]]
    # Structured payload for UIs, e.g. an edit patch and line stats.
    metadata: NotRequired[dict[str, Any]]
    meta: NotRequired[BlockMeta]


type ContentBlock = TextBlock | ThinkingBlock | ImageBlock | DocumentBlock | ToolUseBlock | ToolResultBlock


class ConversationMessage(TypedDict):
    """One timeline record: a user or assistant message, or a compact marker."""

    role: Literal["user", "assistant", "compact"]
    content: list[ContentBlock]
    meta: NotRequired[MessageMeta]


class RewindMeta(TypedDict):
    rewind_to: int
    created_at: str


class RewindMarker(TypedDict):
    """Raw-log record that truncates the visible timeline; see :func:`mycode.session.apply_rewind`."""

    role: Literal["rewind"]
    meta: RewindMeta


def _with_meta[B: ContentBlock](block: B, meta: BlockMeta | None) -> B:
    """Attach a copy of ``meta`` to ``block``, if non-empty."""

    if meta:
        block["meta"] = meta.copy()
    return block


def text_block(text: str, *, meta: BlockMeta | None = None) -> TextBlock:
    return _with_meta({"type": "text", "text": text}, meta)


def thinking_block(text: str, *, meta: BlockMeta | None = None) -> ThinkingBlock:
    return _with_meta({"type": "thinking", "text": text}, meta)


def image_block(
    data: str,
    *,
    mime_type: str,
    name: str | None = None,
    meta: BlockMeta | None = None,
) -> ImageBlock:
    block: ImageBlock = {"type": "image", "data": data, "mime_type": mime_type}
    if name:
        block["name"] = name
    return _with_meta(block, meta)


def document_block(
    data: str,
    *,
    mime_type: str,
    name: str | None = None,
    meta: BlockMeta | None = None,
) -> DocumentBlock:
    block: DocumentBlock = {"type": "document", "data": data, "mime_type": mime_type}
    if name:
        block["name"] = name
    return _with_meta(block, meta)


def tool_use_block(
    *,
    tool_id: str,
    name: str,
    input: dict[str, Any] | None = None,
    meta: BlockMeta | None = None,
) -> ToolUseBlock:
    return _with_meta({"type": "tool_use", "id": tool_id, "name": name, "input": dict(input or {})}, meta)


def tool_result_block(
    *,
    tool_use_id: str,
    output: str,
    metadata: dict[str, Any] | None = None,
    is_error: bool = False,
    content: list[TextBlock | ImageBlock] | None = None,
    meta: BlockMeta | None = None,
) -> ToolResultBlock:
    block: ToolResultBlock = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "output": output,
        "is_error": is_error,
    }
    if metadata:
        block["metadata"] = dict(metadata)
    if content:
        block["content"] = [item.copy() for item in content]
    return _with_meta(block, meta)


def user_text_message(text: str, *, meta: MessageMeta | None = None) -> ConversationMessage:
    return build_message("user", [text_block(text)], meta=meta)


def build_message(
    role: Literal["user", "assistant", "compact"],
    blocks: Sequence[ContentBlock],
    *,
    meta: MessageMeta | None = None,
) -> ConversationMessage:
    message: ConversationMessage = {"role": role, "content": list(blocks)}
    if meta:
        message["meta"] = meta.copy()
    return message


def merge_user_messages(messages: list[ConversationMessage], *, steer: bool) -> ConversationMessage:
    """Merge user messages into one, keeping each item's content blocks in order.

    ``meta.input_ids`` lists each item's ``meta.input_id``; other item meta is
    dropped. ``steer`` sets ``meta.steer`` for a message delivered mid-turn.
    """

    meta: MessageMeta = {"steer": True} if steer else {}
    meta["input_ids"] = [
        input_id for message in messages if (input_id := message.get("meta", {}).get("input_id")) is not None
    ]
    return build_message("user", [block.copy() for message in messages for block in message["content"]], meta=meta)


def assistant_message(
    blocks: list[ContentBlock],
    *,
    provider: str | None = None,
    model: str | None = None,
    provider_message_id: str | None = None,
    stop_reason: StopReason | None = None,
    usage: Usage | None = None,
    cost: Cost | None = None,
    native_meta: dict[str, Any] | None = None,
) -> ConversationMessage:
    """Build a normalized assistant message with shared metadata fields."""

    meta: MessageMeta = {}
    if provider:
        meta["provider"] = provider
    if model:
        meta["model"] = model
    if provider_message_id:
        meta["provider_message_id"] = provider_message_id
    if stop_reason:
        meta["stop_reason"] = stop_reason
    if usage:
        meta["usage"] = usage.copy()
    if cost is not None:
        meta["cost"] = cost.copy()
    if native_meta:
        native = {key: value for key, value in native_meta.items() if value is not None}
        if native:
            meta["native"] = native
    return build_message("assistant", blocks, meta=meta or None)


def flatten_message_text(message: ConversationMessage) -> str:
    """Join a message's non-empty text blocks as paragraphs."""

    return "\n\n".join(
        text for block in message["content"] if block["type"] == "text" and (text := block["text"].strip())
    )
