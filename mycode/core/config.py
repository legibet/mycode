"""Application configuration and provider resolution."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mycode.core.models import ModelMetadata, lookup_model_metadata
from mycode.core.providers import (
    is_supported_provider,
    list_auto_discoverable_providers,
    list_supported_providers,
    provider_api_key_from_env,
    provider_default_models,
    provider_env_api_key_names,
)

_DEFAULT_MYCODE_HOME = "~/.mycode"
_API_KEY_ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    type: str  # internal provider adapter id: anthropic | moonshotai | minimax | …
    models: list[str]  # available model names (no provider prefix)
    api_key: str | None = None
    api_key_env_var: str | None = None
    base_url: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class Settings:
    providers: dict[str, ProviderConfig]
    default_provider: str | None
    default_model: str | None
    port: int
    cwd: str
    workspace_root: str
    config_paths: list[str] = field(default_factory=list)


@dataclass
class _ConfigLayer:
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_provider: str | None = None
    default_model: str | None = None
    config_paths: list[str] = field(default_factory=list)


def resolve_mycode_home() -> Path:
    """Resolve the mycode home directory."""

    raw = os.environ.get("MYCODE_HOME", _DEFAULT_MYCODE_HOME)
    return Path(raw).expanduser().resolve(strict=False)


def resolve_sessions_dir() -> Path:
    """Resolve the default directory used for persisted sessions."""

    return resolve_mycode_home() / "sessions"


def find_workspace_root(cwd: str) -> Path:
    """Resolve the project/workspace root for the current cwd."""

    current = Path(cwd).expanduser().resolve(strict=False)
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return current


def _normalize_models(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_config_api_key(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, None

    cleaned = value.strip()
    if not cleaned:
        return None, None

    match = _API_KEY_ENV_REF_RE.fullmatch(cleaned)
    if match:
        return None, match.group(1)

    return cleaned, None


def _parse_layer(path: Path, data: dict[str, Any]) -> _ConfigLayer:
    providers: dict[str, dict[str, Any]] = {}
    for name, raw in (data.get("providers") or {}).items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue

        provider: dict[str, Any] = {}
        if "type" in raw:
            provider["type"] = raw.get("type") or "anthropic"
        if "models" in raw:
            provider["models"] = _normalize_models(raw.get("models"))
        if "api_key" in raw:
            api_key, api_key_env_var = _parse_config_api_key(raw.get("api_key"))
            provider["api_key"] = api_key
            provider["api_key_env_var"] = api_key_env_var
        if "base_url" in raw:
            provider["base_url"] = raw.get("base_url") or None
        if "reasoning_effort" in raw:
            provider["reasoning_effort"] = raw.get("reasoning_effort") or None
        providers[name] = provider

    default_raw = data.get("default")
    default = default_raw if isinstance(default_raw, dict) else {}
    return _ConfigLayer(
        providers=providers,
        default_provider=default.get("provider") if default and isinstance(default.get("provider"), str) else None,
        default_model=default.get("model") if default and isinstance(default.get("model"), str) else None,
        config_paths=[str(path.resolve(strict=False))],
    )


def _merge_layers(base: _ConfigLayer, override: _ConfigLayer) -> _ConfigLayer:
    providers = {name: dict(config) for name, config in base.providers.items()}
    for name, values in override.providers.items():
        merged = dict(providers.get(name, {}))
        merged.update(values)
        providers[name] = merged

    return _ConfigLayer(
        providers=providers,
        default_provider=override.default_provider if override.default_provider is not None else base.default_provider,
        default_model=override.default_model if override.default_model is not None else base.default_model,
        config_paths=base.config_paths + [path for path in override.config_paths if path not in base.config_paths],
    )


def _candidate_config_paths(cwd: str) -> list[Path]:
    workspace_root = find_workspace_root(cwd)
    return [
        resolve_mycode_home() / "config.json",
        workspace_root / ".mycode" / "config.json",
    ]


def _load_layered_config(cwd: str) -> _ConfigLayer:
    merged = _ConfigLayer()
    for path in _candidate_config_paths(cwd):
        data = _load_json(path)
        if data is None:
            continue
        merged = _merge_layers(merged, _parse_layer(path, data))
    return merged


def _build_providers(raw_providers: dict[str, dict[str, Any]]) -> dict[str, ProviderConfig]:
    providers: dict[str, ProviderConfig] = {}
    for name, raw in raw_providers.items():
        provider_type = str(raw.get("type") or "anthropic")
        models = _normalize_models(raw.get("models")) or list(provider_default_models(provider_type))
        providers[name] = ProviderConfig(
            name=name,
            type=provider_type,
            models=models,
            api_key=raw.get("api_key") or None,
            api_key_env_var=raw.get("api_key_env_var") or None,
            base_url=raw.get("base_url") or None,
            reasoning_effort=raw.get("reasoning_effort") or None,
        )
    return providers


def _config_api_key_from_env_var(provider: ProviderConfig, *, require: bool = False) -> str | None:
    env_name = provider.api_key_env_var
    if not env_name:
        return None

    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value

    if require:
        raise ValueError(f"missing API key env var {env_name!r} referenced by provider {provider.name!r}")

    return None


def provider_has_api_key(provider: ProviderConfig) -> bool:
    """Return whether a configured provider can authenticate right now."""

    if provider.api_key_env_var:
        return bool(_config_api_key_from_env_var(provider))
    return bool(provider_api_key_from_env(provider.type) or provider.api_key)


def get_settings(cwd: str | None = None) -> Settings:
    """Load settings from global + project mycode config files."""

    resolved_cwd = str(Path(cwd or os.getcwd()).expanduser().resolve(strict=False))
    workspace_root = find_workspace_root(resolved_cwd)
    merged = _load_layered_config(resolved_cwd)

    return Settings(
        providers=_build_providers(merged.providers),
        default_provider=merged.default_provider,
        default_model=merged.default_model,
        port=int(os.environ.get("PORT", "8000")),
        cwd=resolved_cwd,
        workspace_root=str(workspace_root),
        config_paths=merged.config_paths,
    )


@dataclass(frozen=True)
class ResolvedProvider:
    """Resolved provider ready for Agent construction."""

    provider: str
    model: str
    api_key: str | None
    api_base: str | None
    reasoning_effort: str | None
    max_tokens: int = 8192
    context_window: int | None = None
    model_metadata: ModelMetadata | None = None
    provider_name: str | None = None

    @property
    def provider_type(self) -> str:
        return self.provider


def resolve_provider(
    settings: Settings,
    *,
    provider_name: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
) -> ResolvedProvider:
    """Resolve provider, model, api_key, and api_base from settings + overrides.

    Used by both CLI and server to avoid duplicating resolution logic.
    """

    selected_provider_name, provider_config = _select_provider(settings, provider_name=provider_name)
    resolved_provider = provider_config.type if provider_config else selected_provider_name

    resolved_model = _resolve_model_name(
        settings,
        selected_provider_name=selected_provider_name,
        provider_type=resolved_provider,
        provider_config=provider_config,
        requested_model=model,
    )

    resolved_api_base = api_base or (provider_config.base_url if provider_config else None)
    model_metadata = lookup_model_metadata(
        provider_type=resolved_provider,
        provider_name=selected_provider_name,
        model=resolved_model,
        api_base=resolved_api_base,
    )
    reasoning_effort = provider_config.reasoning_effort if provider_config else None
    if model_metadata and model_metadata.supports_reasoning is False:
        reasoning_effort = None

    config_env_api_key = _config_api_key_from_env_var(provider_config, require=True) if provider_config else None
    resolved_api_key = api_key or config_env_api_key or provider_api_key_from_env(resolved_provider)
    if not resolved_api_key and provider_config:
        resolved_api_key = provider_config.api_key

    if not resolved_api_key:
        checked = ", ".join(provider_env_api_key_names(resolved_provider)) or "<api key env>"
        raise ValueError(
            f"provider {selected_provider_name!r} is selected but no API key is available; checked: {checked}"
        )

    return ResolvedProvider(
        provider_name=selected_provider_name,
        provider=resolved_provider,
        model=resolved_model,
        api_key=resolved_api_key,
        api_base=resolved_api_base,
        reasoning_effort=reasoning_effort,
        max_tokens=model_metadata.max_output_tokens if model_metadata and model_metadata.max_output_tokens else 8192,
        context_window=model_metadata.context_window if model_metadata else None,
        model_metadata=model_metadata,
    )


def _select_provider(settings: Settings, *, provider_name: str | None) -> tuple[str, ProviderConfig | None]:
    """Select the provider alias/raw id to use for this run.

    Resolution order is:

    1. explicit request provider
    2. configured default provider
    3. first configured provider with available credentials
    4. first auto-discoverable built-in provider with env credentials
    """

    if provider_name:
        return _resolve_provider_reference(settings, provider_name)

    default_provider = (settings.default_provider or "").strip()
    if default_provider:
        return _resolve_provider_reference(settings, default_provider)

    for name, provider in settings.providers.items():
        if provider_has_api_key(provider):
            return name, provider

    for provider_id in list_auto_discoverable_providers():
        if provider_api_key_from_env(provider_id):
            return provider_id, None

    env_names: list[str] = []
    for provider_id in list_auto_discoverable_providers():
        for env_name in provider_env_api_key_names(provider_id):
            if env_name not in env_names:
                env_names.append(env_name)
    checked = ", ".join(env_names) or "<api key env>"
    raise ValueError(
        "no available providers found; set one of the supported API key env vars "
        f"({checked}) or configure a provider in ~/.mycode/config.json or <workspace>/.mycode/config.json"
    )


def _resolve_provider_reference(settings: Settings, provider_name: str) -> tuple[str, ProviderConfig | None]:
    """Resolve a configured alias or a raw built-in provider id."""

    cleaned_name = provider_name.strip()
    provider_config = settings.providers.get(cleaned_name)
    resolved_provider = provider_config.type if provider_config else cleaned_name

    if not is_supported_provider(resolved_provider):
        supported = ", ".join(list_supported_providers())
        raise ValueError(f"unsupported provider {resolved_provider!r}; supported: {supported}")

    return cleaned_name, provider_config


def _resolve_model_name(
    settings: Settings,
    *,
    selected_provider_name: str,
    provider_type: str,
    provider_config: ProviderConfig | None,
    requested_model: str | None,
) -> str:
    explicit = (requested_model or "").strip()
    if explicit:
        return explicit

    if provider_config and provider_config.models:
        return provider_config.models[0]

    if selected_provider_name == settings.default_provider:
        default_model = (settings.default_model or "").strip()
        if default_model:
            return default_model

    provider_defaults = provider_default_models(provider_type)
    if provider_defaults:
        return provider_defaults[0]

    raise ValueError(f"provider {selected_provider_name!r} does not define any default models")


def setup_logging() -> None:
    """Configure default logging."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
