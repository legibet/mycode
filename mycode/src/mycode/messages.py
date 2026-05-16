"""Internal conversation model shared by the runtime, session store, CLI, and UI.

Provider adapters translate this shape to and from provider-specific wire
formats so the agent loop and session store stay provider-agnostic.

Metadata layout:

- assistant message ``meta`` keeps only normalized top-level fields:
  ``provider``, ``model``, ``provider_message_id``, ``stop_reason``,
  ``total_tokens``, ``context_window`` (see docs/sessions.md for
  ``total_tokens`` semantics)
- provider-specific extras live under ``meta.native`` on messages and
  ``block.meta.native`` on blocks
- local display metadata such as ``block.meta.duration_ms`` is never sent
  upstream
"""

from __future__ import annotations

from typing import Any

from mycode.utils import omit_none

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
    output: str,
    metadata: dict[str, Any] | None = None,
    is_error: bool = False,
    content: list[ContentBlock] | None = None,
    meta: dict[str, Any] | None = None,
) -> ContentBlock:
    """Build a tool-result block.

    `output` is replayed back to providers on later turns.
    `content` carries multimodal blocks (e.g. images) that providers should
    replay alongside the text. `metadata` is an optional structured payload
    for UI consumption (e.g. edit patch and line stats).
    """

    block: ContentBlock = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "output": output,
        "is_error": is_error,
    }
    if metadata:
        block["metadata"] = dict(metadata)
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
    total_tokens: int | None = None,
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
    if total_tokens is not None:
        meta["total_tokens"] = total_tokens
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
