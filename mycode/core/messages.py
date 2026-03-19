"""Internal conversation model shared by the runtime, session store, CLI, and UI.

The runtime persists a single message shape everywhere:

- user message: text blocks and tool_result blocks
- assistant message: thinking blocks, text blocks, and tool_use blocks

Provider adapters translate between this internal shape and provider-specific wire
formats. The agent loop and session store should never need to know provider wire
details.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

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
    content: str,
    is_error: bool = False,
    meta: dict[str, Any] | None = None,
) -> ContentBlock:
    block: ContentBlock = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }
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
        native = {key: value for key, value in native_meta.items() if value is not None}
        if native:
            meta["native"] = native
    return build_message("assistant", blocks, meta=meta or None)


def extract_block_text(block: ContentBlock) -> str:
    block_type = block.get("type")
    if block_type in {"text", "thinking"}:
        return str(block.get("text") or "")
    return ""


def flatten_message_text(message: ConversationMessage, *, include_thinking: bool = True) -> str:
    parts: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif include_thinking and block.get("type") == "thinking":
            parts.append(str(block.get("text") or ""))
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()


@dataclass
class PendingBlock:
    """Mutable block state while a provider stream is still in progress."""

    index: int
    block_type: str
    text_parts: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    tool_id: str | None = None
    tool_name: str = ""
    tool_input: dict[str, Any] | None = None
    tool_input_parts: list[str] = field(default_factory=list)


class AssistantMessageBuilder:
    """Collect provider stream fragments into one assistant message.

    Provider streams emit deltas by block index. This builder keeps block order
    stable and stores provider-specific metadata on the relevant block.
    """

    def __init__(self) -> None:
        self._blocks: dict[int, PendingBlock] = {}
        self._message_meta: dict[str, Any] = {}

    def append_text(self, index: int, delta: str) -> None:
        if delta:
            self._ensure_block(index, "text").text_parts.append(delta)

    def append_thinking(self, index: int, delta: str) -> None:
        if delta:
            self._ensure_block(index, "thinking").text_parts.append(delta)

    def set_thinking_meta(self, index: int, **meta: Any) -> None:
        block = self._ensure_block(index, "thinking")
        for key, value in meta.items():
            if value is not None:
                block.meta[key] = value

    def start_tool_use(
        self,
        index: int,
        *,
        tool_id: str | None,
        name: str | None,
        input: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        block = self._ensure_block(index, "tool_use")
        if tool_id:
            block.tool_id = tool_id
        if name:
            block.tool_name = name
        if input is not None:
            block.tool_input = dict(input)
        if meta:
            block.meta.update(meta)

    def append_tool_input(self, index: int, delta: str) -> None:
        if delta:
            self._ensure_block(index, "tool_use").tool_input_parts.append(delta)

    def update_message_meta(self, **meta: Any) -> None:
        for key, value in meta.items():
            if value is not None:
                self._message_meta[key] = value

    @property
    def message_meta(self) -> dict[str, Any]:
        return dict(self._message_meta)

    def build(self) -> ConversationMessage:
        blocks: list[ContentBlock] = []

        for index in sorted(self._blocks):
            block_state = self._blocks[index]

            if block_state.block_type == "text":
                text = "".join(block_state.text_parts)
                if text:
                    blocks.append(text_block(text, meta=block_state.meta or None))
                continue

            if block_state.block_type == "thinking":
                text = "".join(block_state.text_parts)
                if text or block_state.meta:
                    blocks.append(thinking_block(text, meta=block_state.meta or None))
                continue

            if block_state.block_type != "tool_use":
                continue

            tool_input = block_state.tool_input
            if block_state.tool_input_parts:
                streamed_input = _parse_tool_input("".join(block_state.tool_input_parts))
                if streamed_input:
                    tool_input = streamed_input

            blocks.append(
                tool_use_block(
                    tool_id=block_state.tool_id or uuid4().hex,
                    name=block_state.tool_name,
                    input=tool_input or {},
                    meta=block_state.meta or None,
                )
            )

        return build_message("assistant", blocks, meta=self._message_meta or None)

    def _ensure_block(self, index: int, block_type: str) -> PendingBlock:
        current = self._blocks.get(index)
        if current is None:
            current = PendingBlock(index=index, block_type=block_type)
            self._blocks[index] = current
            return current

        if current.block_type != block_type:
            current.block_type = block_type
        return current


def _parse_tool_input(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}
