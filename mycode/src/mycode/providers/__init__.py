"""Provider adapter registry and public import surface."""

from __future__ import annotations

from mycode.providers.anthropic_like import AnthropicAdapter, MiniMaxAdapter, MoonshotAIAdapter
from mycode.providers.base import ProviderAdapter
from mycode.providers.gemini import GoogleGeminiAdapter, GoogleVertexAdapter
from mycode.providers.openai_chat import (
    AlibabaAdapter,
    DeepSeekAdapter,
    OpenAIChatAdapter,
    OpenRouterAdapter,
    ZAIAdapter,
)
from mycode.providers.openai_responses import OpenAIResponsesAdapter, XAIAdapter

_PROVIDERS: dict[str, ProviderAdapter] = {
    adapter.provider_id: adapter
    for adapter in (
        AnthropicAdapter(),
        OpenAIResponsesAdapter(),
        GoogleGeminiAdapter(),
        GoogleVertexAdapter(),
        DeepSeekAdapter(),
        ZAIAdapter(),
        MoonshotAIAdapter(),
        MiniMaxAdapter(),
        OpenRouterAdapter(),
        AlibabaAdapter(),
        XAIAdapter(),
        OpenAIChatAdapter(),
    )
}


def list_supported_providers() -> list[str]:
    """Return all built-in provider ids."""

    return sorted(_PROVIDERS)


def list_env_discoverable_providers() -> list[str]:
    """Return provider ids that can be discovered from env vars alone."""

    return [provider_id for provider_id, adapter in _PROVIDERS.items() if adapter.auto_discoverable]


def is_supported_provider(provider_name: str | None) -> bool:
    """Return whether the given provider id is registered."""

    return bool(provider_name and provider_name in _PROVIDERS)


def get_provider_adapter(provider_name: str) -> ProviderAdapter:
    try:
        return _PROVIDERS[provider_name]
    except KeyError as exc:
        supported = ", ".join(list_supported_providers())
        raise ValueError(f"unsupported provider {provider_name!r}; supported: {supported}") from exc


__all__ = [
    "AlibabaAdapter",
    "AnthropicAdapter",
    "ProviderAdapter",
    "GoogleGeminiAdapter",
    "GoogleVertexAdapter",
    "MiniMaxAdapter",
    "MoonshotAIAdapter",
    "DeepSeekAdapter",
    "OpenAIChatAdapter",
    "OpenAIResponsesAdapter",
    "OpenRouterAdapter",
    "XAIAdapter",
    "ZAIAdapter",
    "get_provider_adapter",
    "list_env_discoverable_providers",
    "is_supported_provider",
    "list_supported_providers",
]
