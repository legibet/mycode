"""Application configuration and provider resolution."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mycode.core.models import ModelMetadata, lookup_model_metadata
from mycode.core.provider_registry import (
    is_supported_provider,
    list_supported_providers,
    provider_api_key_from_env,
    provider_default_models,
)

_DEFAULT_MYCODE_HOME = "~/.mycode"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    type: str  # internal provider adapter id: anthropic | moonshotai | minimax | …
    models: list[str]  # available model names (no provider prefix)
    api_key: str | None = None
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

    @property
    def active_provider(self) -> ProviderConfig | None:
        if not self.default_provider:
            return None
        return self.providers.get(self.default_provider)


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
            provider["api_key"] = raw.get("api_key") or None
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
            base_url=raw.get("base_url") or None,
            reasoning_effort=raw.get("reasoning_effort") or None,
        )
    return providers


def _env_api_key_for_provider(provider_type: str | None) -> str | None:
    return provider_api_key_from_env(provider_type)


def provider_has_api_key(provider: ProviderConfig) -> bool:
    return bool(_env_api_key_for_provider(provider.type) or provider.api_key)


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


_FALLBACK_MODEL = "claude-sonnet-4-6"
_FALLBACK_PROVIDER = "anthropic"


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

    cfg: ProviderConfig | None = None
    if provider_name and provider_name in settings.providers:
        cfg = settings.providers[provider_name]
    elif settings.active_provider:
        cfg = settings.active_provider

    if provider_name:
        resolved_provider = cfg.type if cfg else provider_name
    else:
        resolved_provider = cfg.type if cfg else _FALLBACK_PROVIDER

    if not is_supported_provider(resolved_provider):
        supported = ", ".join(list_supported_providers())
        raise ValueError(f"unsupported provider {resolved_provider!r}; supported: {supported}")

    resolved_model = _resolve_model_name(
        settings,
        provider_name=provider_name,
        provider_type=resolved_provider,
        provider_config=cfg,
        requested_model=model,
    )

    resolved_api_base = api_base or (cfg.base_url if cfg else None)
    model_metadata = lookup_model_metadata(
        provider_type=resolved_provider,
        provider_name=provider_name,
        model=resolved_model,
        api_base=resolved_api_base,
    )
    reasoning_effort = cfg.reasoning_effort if cfg else None
    if model_metadata and model_metadata.supports_reasoning is False:
        reasoning_effort = None

    return ResolvedProvider(
        provider=resolved_provider,
        model=resolved_model,
        api_key=api_key or _env_api_key_for_provider(resolved_provider) or (cfg.api_key if cfg else None),
        api_base=resolved_api_base,
        reasoning_effort=reasoning_effort,
        max_tokens=model_metadata.max_output_tokens if model_metadata and model_metadata.max_output_tokens else 8192,
        context_window=model_metadata.context_window if model_metadata else None,
        model_metadata=model_metadata,
    )


def _resolve_model_name(
    settings: Settings,
    *,
    provider_name: str | None,
    provider_type: str,
    provider_config: ProviderConfig | None,
    requested_model: str | None,
) -> str:
    explicit = (requested_model or "").strip()
    if explicit:
        return explicit

    if provider_config and provider_config.models:
        return provider_config.models[0]

    if not provider_name or provider_name == settings.default_provider:
        default_model = (settings.default_model or "").strip()
        if default_model:
            return default_model

    provider_defaults = provider_default_models(provider_type)
    if provider_defaults:
        return provider_defaults[0]

    return _FALLBACK_MODEL


def setup_logging() -> None:
    """Configure default logging."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
