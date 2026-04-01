"""Shared provider adapter interfaces.

The agent loop talks to providers through a small normalized contract:

- input: `ProviderRequest`
- output: streamed `ProviderStreamEvent` objects

Concrete adapters are free to use the official SDK or protocol that best matches
their upstream provider. Each adapter is also responsible for projecting the
canonical session transcript into a provider-safe replay history before a new
request is sent upstream.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from mycode.core.messages import ConversationMessage, build_message, text_block, tool_result_block

DEFAULT_REQUEST_TIMEOUT = 300.0


@dataclass(frozen=True)
class ProviderRequest:
    provider: str
    model: str
    session_id: str | None
    messages: list[ConversationMessage]
    system: str
    tools: list[dict[str, Any]]
    max_tokens: int
    api_key: str | None
    api_base: str | None
    reasoning_effort: str | None = None
    supports_image_input: bool = True


@dataclass
class ProviderStreamEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


def dump_model(value: Any) -> Any:
    """Convert SDK model objects into plain Python data."""

    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [dump_model(item) for item in value]
    return value


def get_native_meta(block: dict[str, Any]) -> dict[str, Any]:
    """Return block.meta.native as a dict, or {} if absent."""

    raw_meta = block.get("meta")
    if isinstance(raw_meta, dict):
        candidate = raw_meta.get("native")
        if isinstance(candidate, dict):
            return candidate
    return {}


class ProviderAdapter(ABC):
    """Base class for provider adapters.

    New adapters usually only need to implement `stream_turn()` and optionally
    override tool-call ID projection.
    """

    provider_id: str
    label: str
    default_base_url: str | None = None
    env_api_key_names: tuple[str, ...] = ()
    # Used only as lightweight defaults during config resolution.
    default_models: tuple[str, ...] = ()
    # Auto-discovery is intentionally limited to first-party built-ins that can
    # run from environment variables alone.
    auto_discoverable: bool = True
    # Whether this adapter accepts the shared `reasoning_effort` knob. Providers
    # that do not support it keep their upstream default behavior unchanged.
    supports_reasoning_effort: bool = False

    @abstractmethod
    def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        """Stream exactly one assistant turn."""

    def prepare_messages(self, request: ProviderRequest) -> list[ConversationMessage]:
        """Project canonical session history into a provider-safe replay transcript."""

        return _project_messages_for_replay(
            self,
            request.messages,
            supports_image_input=bool(getattr(request, "supports_image_input", True)),
        )

    def project_tool_call_id(self, tool_call_id: str, used_tool_call_ids: set[str]) -> str:
        """Project one canonical tool call ID into a provider-safe ID.

        Most providers accept canonical tool IDs as-is. Adapters can override
        this when the upstream protocol restricts character sets or length, as
        long as the returned ID stays unique within the projected request.
        """

        return tool_call_id

    def api_key_from_env(self) -> str | None:
        import os

        for env_name in self.env_api_key_names:
            value = os.environ.get(env_name)
            if value:
                return value
        return None

    def require_api_key(self, api_key: str | None) -> str:
        resolved = (api_key or "").strip() or self.api_key_from_env() or ""
        if resolved:
            return resolved

        checked = ", ".join(self.env_api_key_names) or "<api key env>"
        raise ValueError(f"missing API key for provider {self.provider_id}; checked: {checked}")

    def resolve_base_url(self, api_base: str | None) -> str | None:
        base = (api_base or self.default_base_url or "").strip()
        return base.rstrip("/") or None


def _project_messages_for_replay(
    adapter: ProviderAdapter,
    source_messages: list[ConversationMessage],
    *,
    supports_image_input: bool,
) -> list[ConversationMessage]:
    """Project canonical transcript messages into one replay-safe transcript."""

    replay_messages: list[ConversationMessage] = []
    tool_id_map: dict[str, str] = {}
    used_tool_call_ids: set[str] = set()
    pending_tool_call_ids: list[str] = []

    def copy_block(block: dict[str, Any]) -> dict[str, Any]:
        copied = dict(block)
        raw_meta = block.get("meta")
        if isinstance(raw_meta, dict):
            copied["meta"] = dict(raw_meta)
        raw_input = block.get("input")
        if isinstance(raw_input, dict):
            copied["input"] = dict(raw_input)
        raw_content = block.get("content")
        if isinstance(raw_content, list):
            copied["content"] = [copy_block(item) for item in raw_content if isinstance(item, dict)]
        return copied

    def flush_interrupted_tool_calls() -> None:
        if not pending_tool_call_ids:
            return

        replay_messages.append(
            build_message(
                "user",
                [
                    tool_result_block(
                        tool_use_id=tool_use_id,
                        model_text="error: tool call was interrupted (no result recorded)",
                        display_text="Tool call was interrupted before it returned a result",
                        is_error=True,
                    )
                    for tool_use_id in pending_tool_call_ids
                ],
            )
        )
        pending_tool_call_ids.clear()

    for message in source_messages:
        role = str(message.get("role") or "")

        if role == "assistant":
            flush_interrupted_tool_calls()

            raw_meta = message.get("meta")
            meta = dict(raw_meta) if isinstance(raw_meta, dict) else None
            if str((meta or {}).get("stop_reason") or "") in {"error", "aborted", "cancelled"}:
                continue

            projected_blocks: list[dict[str, Any]] = []
            for raw_block in message.get("content") or []:
                if not isinstance(raw_block, dict):
                    continue

                block_type = raw_block.get("type")
                if block_type not in {"text", "thinking", "tool_use"}:
                    continue

                projected_block = copy_block(raw_block)
                if block_type == "tool_use":
                    original_id = str(projected_block.get("id") or "")
                    projected_id = tool_id_map.get(original_id, "")
                    if original_id and not projected_id:
                        projected_id = adapter.project_tool_call_id(original_id, used_tool_call_ids)
                        tool_id_map[original_id] = projected_id
                    if projected_id:
                        used_tool_call_ids.add(projected_id)
                        projected_block["id"] = projected_id

                projected_blocks.append(projected_block)

            if not projected_blocks:
                continue

            projected_message = dict(message)
            if meta is not None:
                projected_message["meta"] = meta
            projected_message["content"] = projected_blocks
            replay_messages.append(projected_message)

            pending_tool_call_ids[:] = [
                str(block.get("id") or "")
                for block in projected_blocks
                if block.get("type") == "tool_use" and block.get("id")
            ]
            continue

        if role != "user":
            continue

        projected_blocks: list[dict[str, Any]] = []
        seen_tool_result_ids: set[str] = set()
        has_direct_user_input = False

        for raw_block in message.get("content") or []:
            if not isinstance(raw_block, dict):
                continue

            block_type = raw_block.get("type")
            if block_type == "text":
                text = str(raw_block.get("text") or "")
                if text:
                    projected_blocks.append(copy_block(raw_block))
                    has_direct_user_input = True
                continue

            if block_type == "image":
                if supports_image_input:
                    projected_blocks.append(copy_block(raw_block))
                    has_direct_user_input = True
                continue

            if block_type != "tool_result":
                continue

            projected_block = copy_block(raw_block)
            original_id = str(projected_block.get("tool_use_id") or "")
            projected_block["tool_use_id"] = tool_id_map.get(original_id, original_id)
            if not supports_image_input:
                raw_content = projected_block.get("content")
                if isinstance(raw_content, list):
                    filtered_content = [
                        item for item in raw_content if isinstance(item, dict) and item.get("type") != "image"
                    ]
                    if filtered_content:
                        projected_block["content"] = filtered_content
                    else:
                        projected_block.pop("content", None)
            projected_blocks.append(projected_block)

            tool_use_id = str(projected_block.get("tool_use_id") or "")
            if tool_use_id:
                seen_tool_result_ids.add(tool_use_id)

        if not projected_blocks:
            continue

        if pending_tool_call_ids and has_direct_user_input:
            flush_interrupted_tool_calls()

        projected_message = dict(message)
        raw_meta = message.get("meta")
        if isinstance(raw_meta, dict):
            projected_message["meta"] = dict(raw_meta)
        projected_message["content"] = projected_blocks
        replay_messages.append(projected_message)

        if seen_tool_result_ids:
            pending_tool_call_ids[:] = [
                tool_id for tool_id in pending_tool_call_ids if tool_id not in seen_tool_result_ids
            ]

    flush_interrupted_tool_calls()
    return replay_messages


def load_image_block_payload(block: dict[str, Any]) -> tuple[str, str]:
    """Return (mime_type, base64_data) for one canonical image block."""

    mime_type = block.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type:
        raise ValueError("image block is missing mime_type")

    data = block.get("data")
    if not isinstance(data, str) or not data:
        raise ValueError("image block is missing data")

    return mime_type, data


def tool_result_content_blocks(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Return structured tool-result content, falling back to one text block."""

    raw_content = block.get("content")
    if isinstance(raw_content, list):
        structured = [dict(item) for item in raw_content if isinstance(item, dict)]
        if structured:
            return structured
    return [text_block(str(block.get("model_text") or ""))]
