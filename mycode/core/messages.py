"""Internal conversation model shared by the runtime, session store, CLI, and UI.

The runtime persists a single message shape everywhere:

- user message: text blocks, image blocks, document blocks, and tool_result blocks
- assistant message: thinking blocks, text blocks, and tool_use blocks

Provider adapters translate between this internal shape and provider-specific wire
formats. The agent loop and session store should never need to know provider wire
details.

Metadata contract:

- assistant message `meta` keeps normalized top-level fields only:
  `provider`, `model`, `provider_message_id`, `stop_reason`, `usage`
- provider-specific assistant message extras live under `meta.native`
- provider-specific block replay hints live under `block.meta.native`
"""

from __future__ import annotations

from typing import Any

from mycode.core.utils import omit_none

ContentBlock = dict[str, Any]
ConversationMessage = dict[str, Any]


def text_block(text: str, *, meta: dict[str, Any] | None = None) -> ContentBlock:
    block: ContentBlock = {"type": "text", "text": text}
    if meta:
        block["meta"] = dict(meta)
    return block


def thinking_block(text: str, *, meta: dict[str, Any] | None = None) -> ContentBlock:
    block: ContentBlock = {"type": "thinking", "text": text}
    if meta:
        block["meta"] = dict(meta)
    return block


def image_block(
    data: str,
    *,
    mime_type: str,
    name: str | None = None,
    meta: dict[str, Any] | None = None,
) -> ContentBlock:
    block: ContentBlock = {"type": "image", "data": data, "mime_type": mime_type}
    if name:
        block["name"] = name
    if meta:
        block["meta"] = dict(meta)
    return block


def document_block(
    data: str,
    *,
    mime_type: str,
    name: str | None = None,
    meta: dict[str, Any] | None = None,
) -> ContentBlock:
    block: ContentBlock = {"type": "document", "data": data, "mime_type": mime_type}
    if name:
        block["name"] = name
    if meta:
        block["meta"] = dict(meta)
    return block


def tool_use_block(
    *,
    tool_id: str,
    name: str,
    input: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> ContentBlock:
    block: ContentBlock = {
        "type": "tool_use",
        "id": tool_id,
        "name": name,
        "input": dict(input or {}),
    }
    if meta:
        block["meta"] = dict(meta)
    return block


def tool_result_block(
    *,
    tool_use_id: str,
    model_text: str,
    display_text: str,
    is_error: bool = False,
    content: list[ContentBlock] | None = None,
    meta: dict[str, Any] | None = None,
) -> ContentBlock:
    """Build a tool-result block.

    `model_text` is replayed back to providers on later turns.
    `display_text` is the user-facing text shown by CLI and web UI.
    """

    block: ContentBlock = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "model_text": model_text,
        "display_text": display_text,
        "is_error": is_error,
    }
    if content:
        block["content"] = [dict(item) for item in content]
    if meta:
        block["meta"] = dict(meta)
    return block


def user_text_message(text: str, *, meta: dict[str, Any] | None = None) -> ConversationMessage:
    return build_message("user", [text_block(text)], meta=meta)


def build_message(
    role: str,
    blocks: list[ContentBlock],
    *,
    meta: dict[str, Any] | None = None,
) -> ConversationMessage:
    message: ConversationMessage = {"role": role, "content": blocks}
    if meta:
        message["meta"] = dict(meta)
    return message


def assistant_message(
    blocks: list[ContentBlock],
    *,
    provider: str | None = None,
    model: str | None = None,
    provider_message_id: str | None = None,
    stop_reason: str | None = None,
    usage: Any = None,
    native_meta: dict[str, Any] | None = None,
) -> ConversationMessage:
    """Build a normalized assistant message with shared metadata fields."""

    meta: dict[str, Any] = {}
    if provider:
        meta["provider"] = provider
    if model:
        meta["model"] = model
    if provider_message_id:
        meta["provider_message_id"] = provider_message_id
    if stop_reason:
        meta["stop_reason"] = stop_reason
    if usage is not None:
        meta["usage"] = usage
    if native_meta:
        native = omit_none(native_meta)
        if native:
            meta["native"] = native
    return build_message("assistant", blocks, meta=meta or None)


def flatten_message_text(message: ConversationMessage, *, include_thinking: bool = True) -> str:
    """Flatten readable text while skipping synthetic attachment payload blocks."""

    parts: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        raw_meta = block.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        # Attached file snapshots should not become session titles or history labels.
        if meta.get("attachment"):
            continue
        btype = block.get("type")
        if btype == "text" or (include_thinking and btype == "thinking"):
            parts.append(str(block.get("text") or ""))
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()
