"""Shared provider adapter interfaces.

Each adapter implements `stream_turn()` and reuses `prepare_messages()` to
project the canonical session transcript into a provider-safe replay history
before sending a request upstream.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from mycode.attachments import unsupported_attachment_block
from mycode.compact import apply_compact_replay
from mycode.messages import ConversationMessage, build_message, text_block, tool_result_block

DEFAULT_REQUEST_TIMEOUT = 300.0


class ProviderError(Exception):
    """Normalized provider failure raised by adapters.

    ``reason`` is one of ``connection_error``, ``request_timeout``,
    ``http_status``, ``stream_start_timeout``, ``provider_error``. The agent
    runtime classifies retries on this type only and never inspects
    SDK-specific exceptions.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "provider_error",
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


class StreamStartTimeoutError(ProviderError):
    """No provider stream event arrived within ``stream_start_timeout``."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="stream_start_timeout", retryable=True)


def normalize_provider_error(exc: Exception, provider_id: str) -> ProviderError:
    """Project one SDK exception onto the shared :class:`ProviderError` shape.

    Works on duck-typed attributes shared by the openai/anthropic SDKs
    (``status_code``, ``response``) and google-genai (``code``), plus the httpx
    transport exceptions all three surface directly or as ``__cause__``.
    """

    raw_status = getattr(exc, "status_code", None)
    if raw_status is None:
        raw_status = getattr(exc, "code", None)
    status_code = raw_status if isinstance(raw_status, int) else None

    # TimeoutException is a TransportError subclass — check it first.
    timed_out = isinstance(exc, httpx.TimeoutException) or isinstance(exc.__cause__, httpx.TimeoutException)
    connection_failed = isinstance(exc, httpx.TransportError) or isinstance(exc.__cause__, httpx.TransportError)

    if status_code is not None:
        reason = "http_status"
        retryable = status_code in {408, 409, 429} or status_code >= 500
    elif timed_out:
        reason = "request_timeout"
        retryable = True
    elif connection_failed:
        reason = "connection_error"
        retryable = True
    else:
        reason = "provider_error"
        retryable = False

    # str(httpx.ReadTimeout()) is empty — fall back to type and phase.
    message = str(exc).strip() or f"{type(exc).__name__} while streaming from {provider_id}"

    headers = getattr(getattr(exc, "response", None), "headers", None)
    retry_after: float | None = None
    if headers is not None:
        try:
            retry_after = float(headers.get("retry-after") or "")
        except ValueError:
            retry_after = None

    return ProviderError(
        message,
        reason=reason,
        retryable=retryable,
        status_code=status_code,
        retry_after=retry_after,
    )


@dataclass(frozen=True)
class ProviderRequest:
    provider: str
    model: str
    session_id: str | None
    messages: list[ConversationMessage]
    system: str
    tools: list[dict[str, Any]]
    max_tokens: int
    temperature: float
    api_key: str | None
    api_base: str | None
    reasoning_effort: str | None = None
    supports_image_input: bool = True
    supports_pdf_input: bool = True
    transcript_path: str | None = None
    append_messages: list[ConversationMessage] = field(default_factory=list)
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT


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


def native_block_meta(native: dict[str, Any]) -> dict[str, Any] | None:
    """Wrap accumulated native values as block meta, or None when empty."""

    return {"native": native} if native else None


def _without_native_meta(value: dict[str, Any]) -> dict[str, Any]:
    """Copy a canonical value without provider-specific replay metadata."""

    result = dict(value)
    raw_meta = result.get("meta")
    if not isinstance(raw_meta, dict) or "native" not in raw_meta:
        return result

    meta = dict(raw_meta)
    meta.pop("native")
    if meta:
        result["meta"] = meta
    else:
        result.pop("meta")
    return result


def parse_tool_call_input(raw_arguments: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse a streamed/native tool-call JSON arguments string.

    Returns ``(input, extra_native_fields)``. On parse failure, ``input`` is
    empty and ``extra_native_fields`` preserves the raw text under
    ``raw_arguments`` so it stays inspectable in ``block.meta.native``.
    """

    if not raw_arguments.strip():
        return {}, {}
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {}, {"raw_arguments": raw_arguments}
    if isinstance(parsed, dict):
        return parsed, {}
    return {}, {"raw_arguments": raw_arguments}


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
        """Project visible history into provider-safe replay messages."""

        replay_messages = apply_compact_replay(
            request.messages,
            transcript_path=request.transcript_path,
        )
        if request.append_messages:
            replay_messages = [*replay_messages, *request.append_messages]
        repaired_messages = repair_messages_for_replay(
            replay_messages,
            supports_image_input=request.supports_image_input,
            supports_pdf_input=request.supports_pdf_input,
        )
        prepared_messages: list[ConversationMessage] = []
        tool_id_map: dict[str, str] = {}
        used_tool_call_ids: set[str] = set()

        for message in repaired_messages:
            portable_replay = message.get("role") == "assistant" and not self._can_replay_native_history(
                message, request
            )
            projected_blocks: list[dict[str, Any]] = []
            for raw_block in message.get("content") or []:
                if not isinstance(raw_block, dict):
                    continue

                block = dict(raw_block)
                if portable_replay:
                    if block.get("type") == "thinking":
                        projected_thinking = self._project_incompatible_thinking(block)
                        if projected_thinking is None:
                            continue
                        block = projected_thinking
                    else:
                        block = _without_native_meta(block)

                if block.get("type") == "tool_use":
                    tool_use_id = str(block.get("id") or "")
                    if tool_use_id and tool_use_id not in tool_id_map:
                        tool_id_map[tool_use_id] = self.project_tool_call_id(tool_use_id, used_tool_call_ids)
                        used_tool_call_ids.add(tool_id_map[tool_use_id])
                    if tool_use_id:
                        block["id"] = tool_id_map[tool_use_id]
                elif block.get("type") == "tool_result":
                    tool_use_id = str(block.get("tool_use_id") or "")
                    if tool_use_id in tool_id_map:
                        block["tool_use_id"] = tool_id_map[tool_use_id]

                projected_blocks.append(block)

            projected_message = dict(message)
            projected_message["content"] = projected_blocks
            if portable_replay:
                projected_message = _without_native_meta(projected_message)
            prepared_messages.append(projected_message)

        return prepared_messages

    def _can_replay_native_history(self, message: ConversationMessage, request: ProviderRequest) -> bool:
        """Return whether provider-native history is valid for the target model."""

        raw_meta = message.get("meta")
        return bool(
            isinstance(raw_meta, dict)
            and raw_meta.get("provider") == self.provider_id
            and raw_meta.get("model") == request.model
        )

    def _project_incompatible_thinking(self, block: dict[str, Any]) -> dict[str, Any] | None:
        """Keep readable thinking without replaying incompatible native state."""

        text = str(block.get("text") or "")
        return text_block(text) if text else None

    def project_tool_call_id(self, tool_call_id: str, used_tool_call_ids: set[str]) -> str:
        """Project one canonical tool call ID into a provider-safe ID.

        Most providers accept canonical tool IDs as-is. Adapters can override
        this when the upstream protocol restricts character sets or length, as
        long as the returned ID stays unique within the projected request.
        """
        del used_tool_call_ids
        return tool_call_id

    def api_key_from_env(self) -> str | None:
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


def repair_messages_for_replay(
    source_messages: list[ConversationMessage],
    *,
    supports_image_input: bool,
    supports_pdf_input: bool,
) -> list[ConversationMessage]:
    """Return a minimal replay-safe transcript from canonical session history.

    This keeps only replayable blocks, removes duplicate or orphaned tool
    records, and inserts synthetic error tool results when a tool call was left
    open by an interrupted turn.
    """

    replay_messages: list[ConversationMessage] = []
    emitted_tool_use_ids: set[str] = set()
    emitted_tool_result_ids: set[str] = set()
    open_tool_use_ids: list[str] = []

    for message in source_messages:
        role = str(message.get("role") or "")

        if role == "assistant":
            if open_tool_use_ids:
                replay_messages.append(_interrupted_tool_result_message(open_tool_use_ids))
                emitted_tool_result_ids.update(open_tool_use_ids)
                open_tool_use_ids = []

            raw_meta = message.get("meta")
            stop_reason = str(raw_meta.get("stop_reason") or "") if isinstance(raw_meta, dict) else ""
            if stop_reason in {"error", "aborted", "cancelled"}:
                continue

            content: list[dict[str, Any]] = []
            current_tool_use_ids: list[str] = []
            for raw_block in message.get("content") or []:
                if not isinstance(raw_block, dict):
                    continue
                block_type = raw_block.get("type")
                if block_type in {"text", "thinking"}:
                    text = str(raw_block.get("text") or "")
                    # Empty native thinking blocks may still carry signatures or
                    # provider replay state.
                    if text or get_native_meta(raw_block):
                        content.append(dict(raw_block))
                    continue

                if block_type != "tool_use":
                    continue

                tool_use_id = str(raw_block.get("id") or "")
                if not tool_use_id or tool_use_id in emitted_tool_use_ids:
                    continue

                emitted_tool_use_ids.add(tool_use_id)
                current_tool_use_ids.append(tool_use_id)
                content.append(dict(raw_block))

            if not content:
                continue

            replay_message = dict(message)
            replay_message["content"] = content
            if isinstance(raw_meta, dict):
                replay_message["meta"] = dict(raw_meta)
            replay_messages.append(replay_message)
            open_tool_use_ids = current_tool_use_ids
            continue

        if role != "user":
            continue

        content = []
        resolved_tool_use_ids: set[str] = set()
        has_user_input = False

        for raw_block in message.get("content") or []:
            if not isinstance(raw_block, dict):
                continue

            block_type = raw_block.get("type")
            if block_type == "text":
                text = str(raw_block.get("text") or "")
                if text:
                    has_user_input = True
                    content.append(dict(raw_block))
                continue

            if block_type in {"image", "document"}:
                supported = supports_image_input if block_type == "image" else supports_pdf_input
                has_user_input = True
                if supported:
                    content.append(dict(raw_block))
                else:
                    # Keep a record of the file even though its bytes are dropped.
                    default_mime = "image" if block_type == "image" else "application/pdf"
                    content.append(
                        unsupported_attachment_block(
                            name=str(raw_block.get("name") or f"attached-{block_type}"),
                            mime_type=str(raw_block.get("mime_type") or default_mime),
                            kind=block_type,
                        )
                    )
                continue

            if block_type != "tool_result":
                continue

            tool_use_id = str(raw_block.get("tool_use_id") or "")
            if not tool_use_id or tool_use_id not in emitted_tool_use_ids or tool_use_id in emitted_tool_result_ids:
                continue

            block = dict(raw_block)
            raw_content = block.get("content")
            if not supports_image_input and isinstance(raw_content, list):
                filtered_content = [
                    dict(item) for item in raw_content if isinstance(item, dict) and item.get("type") != "image"
                ]
                if filtered_content:
                    block["content"] = filtered_content
                else:
                    block.pop("content", None)

            content.append(block)
            resolved_tool_use_ids.add(tool_use_id)
            emitted_tool_result_ids.add(tool_use_id)

        if has_user_input and open_tool_use_ids:
            missing_tool_use_ids = [
                tool_use_id for tool_use_id in open_tool_use_ids if tool_use_id not in resolved_tool_use_ids
            ]
            if missing_tool_use_ids:
                replay_messages.append(_interrupted_tool_result_message(missing_tool_use_ids))
                emitted_tool_result_ids.update(missing_tool_use_ids)
            open_tool_use_ids = []

        elif open_tool_use_ids:
            open_tool_use_ids = [
                tool_use_id for tool_use_id in open_tool_use_ids if tool_use_id not in resolved_tool_use_ids
            ]

        if not content:
            if replay_messages and replay_messages[-1].get("role") == "assistant":
                # Keep a valid replay transcript when a corrupted user turn is
                # reduced to nothing after cleanup.
                replay_messages.append(
                    build_message(
                        "user",
                        [text_block("[User turn omitted during replay]")],
                        meta={"synthetic": True},
                    )
                )
            continue

        replay_message = dict(message)
        replay_message["content"] = content
        if isinstance(message.get("meta"), dict):
            replay_message["meta"] = dict(message["meta"])
        replay_messages.append(replay_message)

    if open_tool_use_ids:
        replay_messages.append(_interrupted_tool_result_message(open_tool_use_ids))

    return replay_messages


def _interrupted_tool_result_message(tool_use_ids: list[str]) -> ConversationMessage:
    """Return one synthetic user message that closes interrupted tool calls."""

    return build_message(
        "user",
        [
            tool_result_block(
                tool_use_id=tool_use_id,
                output="error: tool call was interrupted",
                is_error=True,
            )
            for tool_use_id in tool_use_ids
        ],
    )


def load_image_block_payload(block: dict[str, Any]) -> tuple[str, str]:
    """Return (mime_type, base64_data) for one canonical image block."""

    mime_type = block.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type:
        raise ValueError("image block is missing mime_type")

    data = block.get("data")
    if not isinstance(data, str) or not data:
        raise ValueError("image block is missing data")

    return mime_type, data


def load_document_block_payload(block: dict[str, Any]) -> tuple[str, str, str | None]:
    """Return (mime_type, base64_data, name) for one canonical document block."""

    mime_type = block.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type:
        raise ValueError("document block is missing mime_type")

    data = block.get("data")
    if not isinstance(data, str) or not data:
        raise ValueError("document block is missing data")

    name = block.get("name")
    return mime_type, data, name if isinstance(name, str) and name else None


def tool_result_content_blocks(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Return structured tool-result content, falling back to one text block."""

    raw_content = block.get("content")
    if isinstance(raw_content, list):
        structured = [dict(item) for item in raw_content if isinstance(item, dict)]
        if structured:
            return structured
    return [text_block(str(block.get("output") or ""))]
