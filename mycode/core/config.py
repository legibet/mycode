"""Application configuration and provider resolution.

This module keeps runtime config loading intentionally simple:

- read the global config, then the workspace config
- merge provider entries by name
- resolve one runnable provider from explicit args, config defaults, or env
"""

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
    get_provider_adapter,
    is_supported_provider,
    list_env_discoverable_providers,
    list_supported_providers,
    provider_api_key_from_env,
    provider_default_models,
    provider_env_api_key_names,
)

_DEFAULT_MYCODE_HOME = "~/.mycode"
_API_KEY_ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_VALID_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh")


@dataclass(frozen=True)
class ModelConfig:
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_reasoning: bool | None = None
    supports_image_input: bool | None = None


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    type: str  # internal provider adapter id: anthropic | moonshotai | minimax | …
    models: dict[str, ModelConfig]
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
    default_reasoning_effort: str | None = None
    compact_threshold: float | None = None
    config_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedProvider:
    """Resolved provider ready for Agent construction."""

    provider: str
    model: str
    api_key: str | None
    api_base: str | None
    reasoning_effort: str | None
    max_tokens: int = 16_384
    context_window: int | None = 128_000
    supports_reasoning: bool | None = None
    supports_image_input: bool | None = None
    provider_name: str | None = None

    @property
    def provider_type(self) -> str:
        return self.provider


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


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _normalize_models(value: Any) -> dict[str, ModelConfig]:
    if not isinstance(value, dict):
        return {}

    models: dict[str, ModelConfig] = {}
    for model, raw in value.items():
        if not isinstance(model, str):
            continue
        model_id = model.strip()
        if not model_id:
            continue
        if isinstance(raw, ModelConfig):
            models[model_id] = raw
            continue
        raw_config = raw if isinstance(raw, dict) else {}
        context_window = raw_config.get("context_window")
        max_output_tokens = raw_config.get("max_output_tokens")
        supports_reasoning = raw_config.get("supports_reasoning")
        supports_image_input = raw_config.get("supports_image_input")
        models[model_id] = ModelConfig(
            context_window=(
                context_window if isinstance(context_window, int) and not isinstance(context_window, bool) else None
            ),
            max_output_tokens=max_output_tokens
            if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool)
            else None,
            supports_reasoning=supports_reasoning if isinstance(supports_reasoning, bool) else None,
            supports_image_input=supports_image_input if isinstance(supports_image_input, bool) else None,
        )
    return models


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


def _parse_compact_threshold(value: Any) -> float | None:
    """Parse compact_threshold from config.

    Returns ``None`` when the key should keep the current/default value, ``0.0``
    when compaction is explicitly disabled, or a valid float in ``[0, 1]``.
    """

    if value is None:
        return None
    if value is False:
        return 0.0

    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return None

    if threshold < 0 or threshold > 1:
        return None
    return threshold


def normalize_reasoning_effort(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    effort = value.strip().lower()
    if effort in {"", "auto", "default"}:
        return None
    if effort in {"off", "disabled"}:
        return "none"
    return effort


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
    return bool(provider.api_key or provider_api_key_from_env(provider.type))


def _candidate_config_paths(cwd: str) -> list[Path]:
    workspace_root = find_workspace_root(cwd)
    return [
        resolve_mycode_home() / "config.json",
        workspace_root / ".mycode" / "config.json",
    ]


def _build_providers(raw_providers: dict[str, dict[str, Any]]) -> dict[str, ProviderConfig]:
    providers: dict[str, ProviderConfig] = {}

    for name, raw in raw_providers.items():
        raw_type = raw.get("type")
        if raw_type:
            provider_type = str(raw_type)
        elif is_supported_provider(name):
            # Built-in providers can be overridden by name without repeating type.
            provider_type = name
        else:
            raise ValueError(f"provider {name!r} must set 'type'")

        models = _normalize_models(raw.get("models"))
        if not models:
            models = {model: ModelConfig() for model in provider_default_models(provider_type)}
        providers[name] = ProviderConfig(
            name=name,
            type=provider_type,
            models=models,
            api_key=raw.get("api_key") or None,
            api_key_env_var=raw.get("api_key_env_var") or None,
            base_url=raw.get("base_url") or None,
            reasoning_effort=normalize_reasoning_effort(raw.get("reasoning_effort")),
        )

    return providers


def get_settings(cwd: str | None = None) -> Settings:
    """Load settings from global and workspace config files."""

    resolved_cwd = str(Path(cwd or os.getcwd()).expanduser().resolve(strict=False))
    workspace_root = find_workspace_root(resolved_cwd)

    raw_providers: dict[str, dict[str, Any]] = {}
    default_provider: str | None = None
    default_model: str | None = None
    default_reasoning_effort: str | None = None
    compact_threshold: float | None = None
    config_paths: list[str] = []

    for path in _candidate_config_paths(resolved_cwd):
        data = _load_json(path)
        if data is None:
            continue

        resolved_path = str(path.resolve(strict=False))
        if resolved_path not in config_paths:
            config_paths.append(resolved_path)

        for name, raw in (data.get("providers") or {}).items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                continue

            merged = dict(raw_providers.get(name, {}))

            if "type" in raw:
                merged["type"] = raw.get("type") or "anthropic"
            if "models" in raw:
                merged["models"] = _normalize_models(raw.get("models"))
            if "api_key" in raw:
                api_key, api_key_env_var = _parse_config_api_key(raw.get("api_key"))
                merged["api_key"] = api_key
                merged["api_key_env_var"] = api_key_env_var
            if "base_url" in raw:
                merged["base_url"] = raw.get("base_url") or None
            if "reasoning_effort" in raw:
                merged["reasoning_effort"] = raw.get("reasoning_effort") or None

            raw_providers[name] = merged

        default = data.get("default") if isinstance(data.get("default"), dict) else {}
        if "provider" in default:
            value = default.get("provider")
            default_provider = value if isinstance(value, str) else None
        if "model" in default:
            value = default.get("model")
            default_model = value if isinstance(value, str) else None
        if "reasoning_effort" in default:
            value = default.get("reasoning_effort")
            default_reasoning_effort = value if isinstance(value, str) else None
        if "compact_threshold" in default:
            parsed_threshold = _parse_compact_threshold(default.get("compact_threshold"))
            if parsed_threshold is not None:
                compact_threshold = parsed_threshold

    return Settings(
        providers=_build_providers(raw_providers),
        default_provider=default_provider,
        default_model=default_model,
        default_reasoning_effort=normalize_reasoning_effort(default_reasoning_effort),
        compact_threshold=compact_threshold,
        port=int(os.environ.get("PORT", "8000")),
        cwd=resolved_cwd,
        workspace_root=str(workspace_root),
        config_paths=config_paths,
    )


def resolve_provider(
    settings: Settings,
    *,
    provider_name: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
) -> ResolvedProvider:
    """Resolve provider, model, api_key, and api_base from settings and overrides."""

    selected_name = (provider_name or settings.default_provider or "").strip()
    if selected_name:
        return _resolve_provider_runtime(
            settings,
            selected_name=selected_name,
            model=model,
            api_key=api_key,
            api_base=api_base,
        )

    for available_name, _provider in _available_provider_references(settings):
        return _resolve_provider_runtime(
            settings,
            selected_name=available_name,
            model=model,
            api_key=api_key,
            api_base=api_base,
        )

    env_names: list[str] = []
    for provider_id in list_env_discoverable_providers():
        for env_name in provider_env_api_key_names(provider_id):
            if env_name not in env_names:
                env_names.append(env_name)
    checked = ", ".join(env_names) or "<api key env>"
    raise ValueError(
        "no available providers found; set one of the supported API key env vars "
        f"({checked}) or configure a provider in ~/.mycode/config.json or <workspace>/.mycode/config.json"
    )


def resolve_provider_choices(settings: Settings) -> list[ResolvedProvider]:
    """Return currently selectable providers in stable selection order."""

    choices: list[ResolvedProvider] = []
    for selected_name, _provider in _available_provider_references(settings):
        try:
            choices.append(_resolve_provider_runtime(settings, selected_name=selected_name))
        except ValueError:
            continue
    return choices


def _available_provider_references(settings: Settings) -> list[tuple[str, ProviderConfig | None]]:
    """Return usable provider references with the configured default first."""

    available: list[tuple[str, ProviderConfig | None]] = []
    seen: set[str] = set()
    configured_types_with_credentials: set[str] = set()

    def add(name: str | None) -> None:
        cleaned = (name or "").strip()
        if not cleaned or cleaned in seen:
            return

        provider_config = settings.providers.get(cleaned)
        provider_type = provider_config.type if provider_config else cleaned
        if not is_supported_provider(provider_type):
            return

        if provider_config:
            if not provider_has_api_key(provider_config):
                return
            configured_types_with_credentials.add(provider_type)
        elif not provider_api_key_from_env(provider_type):
            return

        seen.add(cleaned)
        available.append((cleaned, provider_config))

    add(settings.default_provider)

    for name, provider in settings.providers.items():
        if provider_has_api_key(provider):
            add(name)

    for provider_id in list_env_discoverable_providers():
        if provider_id in configured_types_with_credentials or not provider_api_key_from_env(provider_id):
            continue
        add(provider_id)

    return available


def _resolve_provider_runtime(
    settings: Settings,
    *,
    selected_name: str,
    model: str | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
) -> ResolvedProvider:
    """Resolve one configured alias or raw provider id into a runnable config."""

    provider_config = settings.providers.get(selected_name)
    provider_type = provider_config.type if provider_config else selected_name

    if not is_supported_provider(provider_type):
        supported = ", ".join(list_supported_providers())
        raise ValueError(f"unsupported provider {provider_type!r}; supported: {supported}")

    requested_model = (model or "").strip()
    if requested_model:
        resolved_model = requested_model
    elif provider_config and provider_config.models:
        resolved_model = next(iter(provider_config.models))
    elif selected_name == settings.default_provider and (settings.default_model or "").strip():
        resolved_model = str(settings.default_model).strip()
    else:
        defaults = provider_default_models(provider_type)
        if not defaults:
            raise ValueError(f"provider {selected_name!r} does not define any default models")
        resolved_model = defaults[0]

    resolved_api_base = api_base or (provider_config.base_url if provider_config else None)
    model_metadata = lookup_model_metadata(
        provider_type=provider_type,
        provider_name=selected_name,
        model=resolved_model,
        api_base=resolved_api_base,
    )
    if provider_config:
        model_config = provider_config.models.get(resolved_model)
        if model_config:
            if model_metadata is None:
                model_metadata = ModelMetadata(
                    provider=provider_type,
                    model=resolved_model,
                    context_window=model_config.context_window,
                    max_output_tokens=model_config.max_output_tokens,
                    supports_reasoning=model_config.supports_reasoning,
                    supports_image_input=model_config.supports_image_input,
                )
            else:
                model_metadata = ModelMetadata(
                    provider=model_metadata.provider,
                    model=model_metadata.model,
                    context_window=(
                        model_config.context_window
                        if model_config.context_window is not None
                        else model_metadata.context_window
                    ),
                    max_output_tokens=(
                        model_config.max_output_tokens
                        if model_config.max_output_tokens is not None
                        else model_metadata.max_output_tokens
                    ),
                    supports_reasoning=(
                        model_config.supports_reasoning
                        if model_config.supports_reasoning is not None
                        else model_metadata.supports_reasoning
                    ),
                    supports_image_input=(
                        model_config.supports_image_input
                        if model_config.supports_image_input is not None
                        else model_metadata.supports_image_input
                    ),
                )

    configured_effort = settings.default_reasoning_effort
    if provider_config and provider_config.reasoning_effort is not None:
        configured_effort = provider_config.reasoning_effort

    if configured_effort is not None and configured_effort not in _VALID_REASONING_EFFORTS:
        supported = ", ".join(_VALID_REASONING_EFFORTS)
        raise ValueError(f"unsupported reasoning_effort {configured_effort!r}; supported: {supported}")

    adapter = get_provider_adapter(provider_type)
    supports_reasoning = model_metadata.supports_reasoning if model_metadata else None
    supports_image_input = (
        adapter.supports_image_input and model_metadata is not None and model_metadata.supports_image_input is True
    )
    if (
        configured_effort is None
        or model_metadata is None
        or supports_reasoning is not True
        or not adapter.supports_reasoning_effort
    ):
        reasoning_effort = None
    else:
        reasoning_effort = configured_effort

    resolved_api_key = api_key
    if not resolved_api_key and provider_config:
        if provider_config.api_key_env_var:
            resolved_api_key = _config_api_key_from_env_var(provider_config, require=True)
        elif provider_config.api_key:
            resolved_api_key = provider_config.api_key
    if not resolved_api_key:
        resolved_api_key = provider_api_key_from_env(provider_type)

    if not resolved_api_key:
        checked = ", ".join(provider_env_api_key_names(provider_type)) or "<api key env>"
        raise ValueError(f"provider {selected_name!r} is selected but no API key is available; checked: {checked}")

    return ResolvedProvider(
        provider_name=selected_name,
        provider=provider_type,
        model=resolved_model,
        api_key=resolved_api_key,
        api_base=resolved_api_base,
        reasoning_effort=reasoning_effort,
        max_tokens=model_metadata.max_output_tokens if model_metadata and model_metadata.max_output_tokens else 16_384,
        context_window=model_metadata.context_window if model_metadata and model_metadata.context_window else 128_000,
        supports_reasoning=supports_reasoning,
        supports_image_input=supports_image_input,
    )


def setup_logging() -> None:
    """Configure default logging."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
