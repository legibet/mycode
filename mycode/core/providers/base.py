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

from mycode.core.messages import ConversationMessage, build_message, tool_result_block

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

        return _ReplayProjector(self, request).run()

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


@dataclass
class _ReplayProjector:
    """Project canonical transcript messages into one replay-safe transcript."""

    adapter: ProviderAdapter
    request: ProviderRequest
    messages: list[ConversationMessage] = field(default_factory=list)
    tool_id_map: dict[str, str] = field(default_factory=dict)
    used_tool_call_ids: set[str] = field(default_factory=set)
    pending_tool_call_ids: list[str] = field(default_factory=list)

    def run(self) -> list[ConversationMessage]:
        for message in self.request.messages:
            role = str(message.get("role") or "")

            if role == "assistant":
                self._flush_interrupted_tool_calls()
                projected_message = self._project_assistant_message(message)
                if projected_message is None:
                    continue

                self.messages.append(projected_message)
                self.pending_tool_call_ids = [
                    str(block.get("id") or "")
                    for block in projected_message.get("content") or []
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
                ]
                continue

            if role != "user":
                continue

            projected_message, seen_tool_result_ids, has_text = self._project_user_message(message)
            if projected_message is None:
                continue

            if self.pending_tool_call_ids and has_text:
                self._flush_interrupted_tool_calls()

            self.messages.append(projected_message)
            if seen_tool_result_ids:
                self.pending_tool_call_ids = [
                    tool_id for tool_id in self.pending_tool_call_ids if tool_id not in seen_tool_result_ids
                ]

        self._flush_interrupted_tool_calls()
        return self.messages

    def _project_assistant_message(self, message: ConversationMessage) -> ConversationMessage | None:
        raw_meta = message.get("meta")
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else None
        if str((meta or {}).get("stop_reason") or "") in {"error", "aborted", "cancelled"}:
            return None

        projected_blocks: list[dict[str, Any]] = []

        for raw_block in message.get("content") or []:
            if not isinstance(raw_block, dict):
                continue

            block_type = raw_block.get("type")
            if block_type not in {"text", "thinking", "tool_use"}:
                continue

            projected_block = self._copy_block(raw_block)
            if block_type == "tool_use":
                original_id = str(projected_block.get("id") or "")
                projected_id = self.tool_id_map.get(original_id, "")
                if original_id and not projected_id:
                    projected_id = self.adapter.project_tool_call_id(original_id, self.used_tool_call_ids)
                    self.tool_id_map[original_id] = projected_id
                if projected_id:
                    self.used_tool_call_ids.add(projected_id)
                    projected_block["id"] = projected_id
            projected_blocks.append(projected_block)

        if not projected_blocks:
            return None

        projected_message = dict(message)
        if meta is not None:
            projected_message["meta"] = meta
        projected_message["content"] = projected_blocks
        return projected_message

    def _project_user_message(self, message: ConversationMessage) -> tuple[ConversationMessage | None, set[str], bool]:
        projected_blocks: list[dict[str, Any]] = []
        seen_tool_result_ids: set[str] = set()
        has_text = False

        for raw_block in message.get("content") or []:
            if not isinstance(raw_block, dict):
                continue

            block_type = raw_block.get("type")
            if block_type == "text":
                text = str(raw_block.get("text") or "")
                if text:
                    projected_blocks.append(self._copy_block(raw_block))
                    has_text = True
                continue

            if block_type != "tool_result":
                continue

            projected_block = self._copy_block(raw_block)
            original_id = str(projected_block.get("tool_use_id") or "")
            projected_block["tool_use_id"] = self.tool_id_map.get(original_id, original_id)
            projected_blocks.append(projected_block)
            if projected_block["tool_use_id"]:
                seen_tool_result_ids.add(str(projected_block["tool_use_id"]))

        if not projected_blocks:
            return None, set(), False

        projected_message = dict(message)
        raw_meta = message.get("meta")
        if isinstance(raw_meta, dict):
            projected_message["meta"] = dict(raw_meta)
        projected_message["content"] = projected_blocks
        return projected_message, seen_tool_result_ids, has_text

    def _flush_interrupted_tool_calls(self) -> None:
        if not self.pending_tool_call_ids:
            return

        self.messages.append(
            build_message(
                "user",
                [
                    tool_result_block(
                        tool_use_id=tool_use_id,
                        model_text="error: tool call was interrupted (no result recorded)",
                        display_text="Tool call was interrupted before it returned a result",
                        is_error=True,
                    )
                    for tool_use_id in self.pending_tool_call_ids
                ],
            )
        )
        self.pending_tool_call_ids = []

    def _copy_block(self, block: dict[str, Any]) -> dict[str, Any]:
        copied = dict(block)
        raw_meta = block.get("meta")
        if isinstance(raw_meta, dict):
            copied["meta"] = dict(raw_meta)
        raw_input = block.get("input")
        if isinstance(raw_input, dict):
            copied["input"] = dict(raw_input)
        return copied
