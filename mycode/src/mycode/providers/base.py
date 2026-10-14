"""Shared provider adapter interfaces.

Each adapter implements `stream_turn()` and reuses `prepare_messages()` to
project the canonical session transcript into a provider-safe replay history
before sending a request upstream.
"""

from __future__ import annotations

import copy
import json
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2

from mycode.attachments import unsupported_attachment_block
from mycode.compact import apply_compact_replay
from mycode.messages import (
    BlockMeta,
    ContentBlock,
    ConversationMessage,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    build_message,
    text_block,
    tool_result_block,
)
from mycode.tools import ToolDefinition

DEFAULT_REQUEST_TIMEOUT = 300.0


type ProviderErrorReason = Literal[
    "connection_error", "request_timeout", "http_status", "stream_start_timeout", "provider_error"
]


class ProviderError(Exception):
    """Normalized provider failure raised by adapters.

    The agent runtime classifies retries on this type only and never inspects
    SDK-specific exceptions.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: ProviderErrorReason = "provider_error",
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
    (``status_code``, ``response``) and google-genai (``code``), plus the HTTPX2
    transport exceptions all three surface directly or as ``__cause__``.
    """

    raw_status = getattr(exc, "status_code", None)
    if raw_status is None:
        raw_status = getattr(exc, "code", None)
    status_code = raw_status if isinstance(raw_status, int) and 400 <= raw_status <= 599 else None

    # TimeoutException is a TransportError subclass — check it first.
    timed_out = isinstance(exc, httpx2.TimeoutException) or isinstance(exc.__cause__, httpx2.TimeoutException)
    connection_failed = isinstance(exc, httpx2.TransportError) or isinstance(exc.__cause__, httpx2.TransportError)

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

    # str(httpx2.ReadTimeout()) is empty — fall back to type and phase.
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
    tools: list[ToolDefinition]
    max_tokens: int
    api_key: str | None
    api_base: str | None
    reasoning_effort: str | None = None
    legacy_max_tokens: bool = False
    supports_image_input: bool = True
    supports_pdf_input: bool = True
    transcript_path: str | None = None
    append_messages: list[ConversationMessage] = field(default_factory=list)
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT


@dataclass(frozen=True, slots=True)
class StreamStarted:
    """The upstream stream produced its first event, ending the stream-start deadline."""


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class MessageDone:
    """The turn's final assistant message, with all blocks and metadata."""

    message: ConversationMessage


type ProviderStreamEvent = StreamStarted | ThinkingDelta | TextDelta | MessageDone


def dump_model(value: Any) -> Any:
    """Convert SDK model objects into plain Python data, keyed by wire names."""

    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True)
    if isinstance(value, list):
        return [dump_model(item) for item in value]
    return value


def get_native_meta(block: ContentBlock) -> dict[str, Any]:
    """Return ``block.meta.native``, or {} if absent."""

    return block.get("meta", {}).get("native", {})


def native_block_meta(native: dict[str, Any]) -> BlockMeta | None:
    """Wrap accumulated native values as block meta, or None when empty."""

    return {"native": native} if native else None


def _without_native_meta[T: ContentBlock | ConversationMessage](value: T) -> T:
    """Copy a canonical message or block without provider-specific replay metadata."""

    result = copy.copy(value)
    if "meta" in result:
        meta = result["meta"].copy()
        meta.pop("native", None)
        if meta:
            result["meta"] = meta
        else:
            del result["meta"]
    return result


def parse_tool_call_input(raw_arguments: str) -> tuple[dict[str, Any], BlockMeta]:
    """Parse a streamed/native tool-call JSON arguments string.

    Returns ``(input, invalid_meta)``. On parse failure, ``input`` is empty and
    the canonical block meta fields ``invalid_input``, ``raw_arguments``, and
    ``parse_error`` carry what the model needs to correct the call.
    """

    if not raw_arguments or raw_arguments.isspace():
        return {}, {}
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        return {}, {"invalid_input": True, "raw_arguments": raw_arguments, "parse_error": str(exc)}
    if isinstance(parsed, dict):
        return parsed, {}
    return {}, {
        "invalid_input": True,
        "raw_arguments": raw_arguments,
        "parse_error": f"arguments are not a JSON object: {type(parsed).__name__}",
    }


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
            portable_replay = message["role"] == "assistant" and not self._can_replay_native_history(message, request)
            projected_blocks: list[ContentBlock] = []
            for block in message["content"]:
                if portable_replay:
                    if block["type"] == "thinking":
                        projected_thinking = self._project_incompatible_thinking(block)
                        if projected_thinking is None:
                            continue
                        block = projected_thinking
                    else:
                        block = _without_native_meta(block)

                if block["type"] == "tool_use":
                    tool_use_id = block["id"]
                    if tool_use_id not in tool_id_map:
                        tool_id_map[tool_use_id] = self.project_tool_call_id(tool_use_id, used_tool_call_ids)
                        used_tool_call_ids.add(tool_id_map[tool_use_id])
                    block["id"] = tool_id_map[tool_use_id]
                elif block["type"] == "tool_result" and block["tool_use_id"] in tool_id_map:
                    block["tool_use_id"] = tool_id_map[block["tool_use_id"]]

                projected_blocks.append(block)

            message["content"] = projected_blocks
            if portable_replay:
                message = _without_native_meta(message)
            prepared_messages.append(message)

        return prepared_messages

    def _can_replay_native_history(self, message: ConversationMessage, request: ProviderRequest) -> bool:
        """Return whether provider-native history is valid for the target model."""

        meta = message.get("meta", {})
        return meta.get("provider") == self.provider_id and meta.get("model") == request.model

    def _project_incompatible_thinking(self, block: ThinkingBlock) -> ContentBlock | None:
        """Keep readable thinking without replaying incompatible native state."""

        return text_block(block["text"]) if block["text"] else None

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

    def can_authenticate_from_env(self) -> bool:
        """Return whether ambient environment state can authenticate this adapter."""

        return self.api_key_from_env() is not None

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
        content: list[ContentBlock] = []
        if message["role"] == "assistant":
            if open_tool_use_ids:
                replay_messages.append(_interrupted_tool_result_message(open_tool_use_ids))
                emitted_tool_result_ids.update(open_tool_use_ids)
                open_tool_use_ids = []

            if message.get("meta", {}).get("stop_reason") in {"error", "cancelled"}:
                continue

            current_tool_use_ids: list[str] = []
            for block in message["content"]:
                match block:
                    case {"type": "text" | "thinking"}:
                        # Empty native thinking blocks may still carry signatures or
                        # provider replay state.
                        if block["text"] or get_native_meta(block):
                            content.append(block.copy())
                    case {"type": "tool_use"}:
                        # Some compatible upstreams return calls without an id; such a
                        # call cannot be matched to a result.
                        tool_use_id = block["id"]
                        if not tool_use_id or tool_use_id in emitted_tool_use_ids:
                            continue
                        emitted_tool_use_ids.add(tool_use_id)
                        current_tool_use_ids.append(tool_use_id)
                        content.append(block.copy())

            if content:
                replay_messages.append(message | {"content": content})
                open_tool_use_ids = current_tool_use_ids
            continue

        resolved_tool_use_ids: set[str] = set()
        has_user_input = False

        for block in message["content"]:
            match block:
                case {"type": "text"}:
                    if block["text"]:
                        has_user_input = True
                        content.append(block.copy())
                case {"type": "image" | "document"}:
                    has_user_input = True
                    if supports_image_input if block["type"] == "image" else supports_pdf_input:
                        content.append(block.copy())
                    else:
                        # Keep a record of the file even though its bytes are dropped.
                        content.append(
                            unsupported_attachment_block(
                                name=block.get("name") or f"attached-{block['type']}",
                                mime_type=block["mime_type"],
                                kind=block["type"],
                            )
                        )
                case {"type": "tool_result"}:
                    tool_use_id = block["tool_use_id"]
                    if tool_use_id not in emitted_tool_use_ids or tool_use_id in emitted_tool_result_ids:
                        continue
                    result = block.copy()
                    if not supports_image_input and "content" in result:
                        kept: list[TextBlock | ImageBlock] = [
                            item for item in result.pop("content") if item["type"] != "image"
                        ]
                        if kept:
                            result["content"] = kept
                    content.append(result)
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
            if replay_messages and replay_messages[-1]["role"] == "assistant":
                # Keep a valid replay transcript when a corrupted user turn is
                # reduced to nothing after cleanup.
                replay_messages.append(build_message("user", [text_block("[User turn omitted during replay]")]))
            continue

        replay_messages.append(message | {"content": content})

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


def tool_result_content_blocks(block: ToolResultBlock) -> list[TextBlock | ImageBlock]:
    """Return the tool result's structured content, or its output as one text block."""

    return block.get("content") or [text_block(block["output"])]
