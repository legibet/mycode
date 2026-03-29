"""Built-in provider adapters and registry helpers.

This package keeps the concrete adapters in separate files, but the registry
and lookup helpers live here so callers have one obvious import surface.
"""

from __future__ import annotations

from mycode.core.providers.anthropic_like import AnthropicAdapter, MiniMaxAdapter, MoonshotAIAdapter
from mycode.core.providers.base import ProviderAdapter
from mycode.core.providers.gemini import GoogleGeminiAdapter
from mycode.core.providers.openai_chat import DeepSeekAdapter, OpenAIChatAdapter, OpenRouterAdapter, ZAIAdapter
from mycode.core.providers.openai_responses import OpenAIResponsesAdapter

_ADAPTERS: dict[str, ProviderAdapter] = {
    adapter.provider_id: adapter
    for adapter in (
        AnthropicAdapter(),
        OpenAIResponsesAdapter(),
        GoogleGeminiAdapter(),
        DeepSeekAdapter(),
        ZAIAdapter(),
        MoonshotAIAdapter(),
        MiniMaxAdapter(),
        OpenRouterAdapter(),
        OpenAIChatAdapter(),
    )
}


def list_supported_providers() -> list[str]:
    return sorted(_ADAPTERS)


def list_env_discoverable_providers() -> list[str]:
    """Return provider ids that can be discovered from env vars alone."""

    return [provider_id for provider_id, adapter in _ADAPTERS.items() if adapter.auto_discoverable]


def is_supported_provider(provider_name: str | None) -> bool:
    return bool(provider_name and provider_name in _ADAPTERS)


def get_provider_adapter(provider_name: str) -> ProviderAdapter:
    try:
        return _ADAPTERS[provider_name]
    except KeyError as exc:
        supported = ", ".join(list_supported_providers())
        raise ValueError(f"unsupported provider {provider_name!r}; supported: {supported}") from exc


def provider_env_api_key_names(provider_name: str | None) -> tuple[str, ...]:
    if not provider_name or provider_name not in _ADAPTERS:
        return ()
    return _ADAPTERS[provider_name].env_api_key_names


def provider_api_key_from_env(provider_name: str | None) -> str | None:
    if not provider_name or provider_name not in _ADAPTERS:
        return None
    return _ADAPTERS[provider_name].api_key_from_env()


def provider_default_models(provider_name: str | None) -> tuple[str, ...]:
    if not provider_name or provider_name not in _ADAPTERS:
        return ()
    return _ADAPTERS[provider_name].default_models


__all__ = [
    "AnthropicAdapter",
    "ProviderAdapter",
    "GoogleGeminiAdapter",
    "MiniMaxAdapter",
    "MoonshotAIAdapter",
    "DeepSeekAdapter",
    "OpenAIChatAdapter",
    "OpenAIResponsesAdapter",
    "OpenRouterAdapter",
    "ZAIAdapter",
    "get_provider_adapter",
    "list_env_discoverable_providers",
    "is_supported_provider",
    "list_supported_providers",
    "provider_api_key_from_env",
    "provider_default_models",
    "provider_env_api_key_names",
]
