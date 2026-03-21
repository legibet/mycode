"""Shared provider adapter interfaces.

The agent loop talks to providers through a small normalized contract:

- input: `ProviderRequest`
- output: streamed `ProviderStreamEvent` objects

Concrete adapters are free to use the official SDK or protocol that best matches
their upstream provider, as long as they preserve provider-native data when
building the final internal assistant message.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from mycode.core.messages import ConversationMessage

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
    supports_reasoning: bool = False


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
