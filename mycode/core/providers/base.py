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


class ProviderAdapter(ABC):
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
        """Return a provider-safe replay transcript.

        The persisted session history stays canonical and provider-agnostic.
        Before each upstream call, adapters project that history into a replay
        form that closes interrupted tool loops, drops incomplete assistant
        turns, and keeps native thinking only when it is safe to reuse.
        """

        prepared: list[ConversationMessage] = []
        tool_id_map: dict[str, str] = {}
        pending_tool_use_ids: list[str] = []

        for message in request.messages:
            role = str(message.get("role") or "")

            if role == "assistant":
                if pending_tool_use_ids:
                    prepared.append(self._interrupted_tool_result_message(pending_tool_use_ids))
                    pending_tool_use_ids = []

                prepared_message = self._prepare_assistant_message(message, request, tool_id_map)
                if prepared_message is None:
                    continue

                prepared.append(prepared_message)
                pending_tool_use_ids = [
                    str(block.get("id") or "")
                    for block in prepared_message.get("content") or []
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
                ]
                continue

            if role != "user":
                continue

            prepared_message, seen_tool_result_ids, has_text = self._prepare_user_message(message, tool_id_map)
            if prepared_message is None:
                continue

            if pending_tool_use_ids and has_text:
                prepared.append(self._interrupted_tool_result_message(pending_tool_use_ids))
                pending_tool_use_ids = []

            prepared.append(prepared_message)

            if seen_tool_result_ids:
                pending_tool_use_ids = [
                    tool_id for tool_id in pending_tool_use_ids if tool_id not in seen_tool_result_ids
                ]

        if pending_tool_use_ids:
            prepared.append(self._interrupted_tool_result_message(pending_tool_use_ids))

        return prepared

    def normalize_tool_call_id(self, tool_call_id: str) -> str:
        """Return a provider-safe tool call ID.

        Most providers accept our canonical IDs as-is. Adapters can override
        this when their upstream protocol restricts character sets or length.
        """

        return tool_call_id

    def can_replay_assistant_state(self, message: ConversationMessage, request: ProviderRequest) -> bool:
        """Return whether provider-native assistant state can be replayed.

        Cross-provider and cross-model handoffs should be conservative. Native
        thinking/signature state is only reused when the target adapter and
        model exactly match the original assistant turn.
        """

        raw_meta = message.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        return meta.get("provider") == self.provider_id and meta.get("model") == request.model

    def keep_thinking_as_text(self, message: ConversationMessage) -> bool:
        """Return whether readable thinking should be downgraded to text.

        We only carry thinking across handoffs when it is needed to explain a
        tool-using assistant turn that otherwise has no visible text.
        """

        blocks = [block for block in message.get("content") or [] if isinstance(block, dict)]
        has_tool_use = any(block.get("type") == "tool_use" for block in blocks)
        has_visible_text = any(block.get("type") == "text" and str(block.get("text") or "").strip() for block in blocks)
        return has_tool_use and not has_visible_text

    def _prepare_assistant_message(
        self,
        message: ConversationMessage,
        request: ProviderRequest,
        tool_id_map: dict[str, str],
    ) -> ConversationMessage | None:
        """Copy one assistant message into replay form."""

        raw_meta = message.get("meta")
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else None
        if str((meta or {}).get("stop_reason") or "") in {"error", "aborted", "cancelled"}:
            return None

        keep_native_state = self.can_replay_assistant_state(message, request)
        downgrade_thinking = not keep_native_state and self.keep_thinking_as_text(message)
        prepared_blocks: list[dict[str, Any]] = []

        for raw_block in message.get("content") or []:
            if not isinstance(raw_block, dict):
                continue

            block_type = raw_block.get("type")

            if block_type == "text":
                text = str(raw_block.get("text") or "")
                if text:
                    block = dict(raw_block)
                    block_meta = raw_block.get("meta")
                    if isinstance(block_meta, dict):
                        block["meta"] = dict(block_meta)
                    prepared_blocks.append(block)
                continue

            if block_type == "thinking":
                thinking = str(raw_block.get("text") or "")
                if keep_native_state:
                    if thinking or isinstance(raw_block.get("meta"), dict):
                        block = dict(raw_block)
                        block_meta = raw_block.get("meta")
                        if isinstance(block_meta, dict):
                            block["meta"] = dict(block_meta)
                        prepared_blocks.append(block)
                elif downgrade_thinking and thinking:
                    prepared_blocks.append(text_block(thinking))
                continue

            if block_type != "tool_use":
                continue

            block = dict(raw_block)
            block_meta = raw_block.get("meta")
            if isinstance(block_meta, dict):
                block["meta"] = dict(block_meta)
            raw_input = raw_block.get("input")
            if isinstance(raw_input, dict):
                block["input"] = dict(raw_input)

            original_id = str(block.get("id") or "")
            normalized_id = self.normalize_tool_call_id(original_id)
            if original_id and normalized_id != original_id:
                tool_id_map[original_id] = normalized_id
                block["id"] = normalized_id
            prepared_blocks.append(block)

        if not prepared_blocks:
            return None

        prepared_message = dict(message)
        if meta is not None:
            prepared_message["meta"] = meta
        prepared_message["content"] = prepared_blocks
        return prepared_message

    def _prepare_user_message(
        self,
        message: ConversationMessage,
        tool_id_map: dict[str, str],
    ) -> tuple[ConversationMessage | None, set[str], bool]:
        """Copy one user message into replay form."""

        prepared_blocks: list[dict[str, Any]] = []
        seen_tool_result_ids: set[str] = set()
        has_text = False

        for raw_block in message.get("content") or []:
            if not isinstance(raw_block, dict):
                continue

            block_type = raw_block.get("type")
            if block_type == "text":
                text = str(raw_block.get("text") or "")
                if text:
                    block = dict(raw_block)
                    block_meta = raw_block.get("meta")
                    if isinstance(block_meta, dict):
                        block["meta"] = dict(block_meta)
                    prepared_blocks.append(block)
                    has_text = True
                continue

            if block_type != "tool_result":
                continue

            block = dict(raw_block)
            block_meta = raw_block.get("meta")
            if isinstance(block_meta, dict):
                block["meta"] = dict(block_meta)
            original_id = str(block.get("tool_use_id") or "")
            block["tool_use_id"] = tool_id_map.get(original_id, original_id)
            prepared_blocks.append(block)
            if block["tool_use_id"]:
                seen_tool_result_ids.add(str(block["tool_use_id"]))

        if not prepared_blocks:
            return None, set(), False

        prepared_message = dict(message)
        raw_meta = message.get("meta")
        if isinstance(raw_meta, dict):
            prepared_message["meta"] = dict(raw_meta)
        prepared_message["content"] = prepared_blocks
        return prepared_message, seen_tool_result_ids, has_text

    def _interrupted_tool_result_message(self, tool_use_ids: list[str]) -> ConversationMessage:
        """Close an interrupted tool loop without mutating persisted session data."""

        return build_message(
            "user",
            [
                tool_result_block(
                    tool_use_id=tool_use_id,
                    content="error: tool call was interrupted (no result recorded)",
                    is_error=True,
                )
                for tool_use_id in tool_use_ids
            ],
        )

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
