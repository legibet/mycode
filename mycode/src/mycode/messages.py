"""Internal conversation model shared by the runtime, session store, CLI, and UI.

Provider adapters translate this shape to and from provider-specific wire
formats so the agent loop and session store stay provider-agnostic.

Metadata layout:

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

from typing import Any

ContentBlock = dict[str, Any]
ConversationMessage = dict[str, Any]


def _set_meta(block: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
    """Attach a copy of ``meta`` under the shared ``meta`` key, if non-empty."""

    if meta:
        block["meta"] = dict(meta)
    return block


def text_block(text: str, *, meta: dict[str, Any] | None = None) -> ContentBlock:
    return _set_meta({"type": "text", "text": text}, meta)


def thinking_block(text: str, *, meta: dict[str, Any] | None = None) -> ContentBlock:
    return _set_meta({"type": "thinking", "text": text}, meta)


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
    return _set_meta(block, meta)


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
    return _set_meta(block, meta)


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
    return _set_meta(block, meta)


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
    return _set_meta(block, meta)


def user_text_message(text: str, *, meta: dict[str, Any] | None = None) -> ConversationMessage:
    return build_message("user", [text_block(text)], meta=meta)


def build_message(
    role: str,
    blocks: list[ContentBlock],
    *,
    meta: dict[str, Any] | None = None,
) -> ConversationMessage:
    return _set_meta({"role": role, "content": blocks}, meta)


USAGE_TOKEN_KEYS = (
    "total_tokens",
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
)


def build_usage(
    *,
    total_tokens: int | None = None,
    input_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
) -> dict[str, Any]:
    """Build a canonical usage dict from one provider request.

    ``input_tokens`` is the full effective input including cache reads and
    writes; ``output_tokens`` includes reasoning; ``cache_*_tokens`` and
    ``reasoning_tokens`` are subsets of their respective totals. A missing key
    means the upstream did not report it — readers must not substitute 0.
    """

    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    usage = {
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
    }
    return {key: value for key, value in usage.items() if value is not None}


def assistant_message(
    blocks: list[ContentBlock],
    *,
    provider: str | None = None,
    model: str | None = None,
    provider_message_id: str | None = None,
    stop_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    cost: dict[str, float] | None = None,
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
    if usage:
        meta["usage"] = dict(usage)
    if cost is not None:
        meta["cost"] = dict(cost)
    if native_meta:
        native = {key: value for key, value in native_meta.items() if value is not None}
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
        # Local payload blocks should not become session titles or history labels.
        if meta.get("attachment") or meta.get("skill_snapshot"):
            continue
        btype = block.get("type")
        if btype == "text" or (include_thinking and btype == "thinking"):
            parts.append(str(block.get("text") or ""))
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()
