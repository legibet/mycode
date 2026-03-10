"""Application configuration and logging setup."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_MYCODE_HOME = "~/.mycode"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    type: str  # maps directly to any_llm provider: openai | anthropic | gemini | …
    models: list[str]  # available model names (no provider prefix)
    api_key: str | None = None
    base_url: str | None = None


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
            provider["type"] = raw.get("type") or "openai"
        if "models" in raw:
            provider["models"] = _normalize_models(raw.get("models"))
        if "api_key" in raw:
            provider["api_key"] = raw.get("api_key") or None
        if "base_url" in raw:
            provider["base_url"] = raw.get("base_url") or None
        providers[name] = provider

    default = data.get("default") if isinstance(data.get("default"), dict) else {}
    return _ConfigLayer(
        providers=providers,
        default_provider=default.get("provider") if isinstance(default.get("provider"), str) else None,
        default_model=default.get("model") if isinstance(default.get("model"), str) else None,
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
        providers[name] = ProviderConfig(
            name=name,
            type=str(raw.get("type") or "openai"),
            models=_normalize_models(raw.get("models")),
            api_key=raw.get("api_key") or None,
            base_url=raw.get("base_url") or None,
        )
    return providers


def get_settings(cwd: str | None = None) -> Settings:
    """Load settings from global + project mycode config files."""

    resolved_cwd = str(Path(cwd or os.getcwd()).expanduser().resolve(strict=False))
    workspace_root = find_workspace_root(resolved_cwd)
    merged = _load_layered_config(resolved_cwd)
    providers = _build_providers(merged.providers)
    default_provider = merged.default_provider
    default_model = merged.default_model

    if not providers:
        env_model_raw = os.environ.get("MODEL", "")
        env_base = os.environ.get("BASE_URL")
        env_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")

        if ":" in env_model_raw:
            provider_type, env_model = env_model_raw.split(":", 1)
        elif "/" in env_model_raw:
            provider_type, env_model = env_model_raw.split("/", 1)
        else:
            provider_type, env_model = "openai", env_model_raw

        if env_model:
            providers["env"] = ProviderConfig(
                name="env",
                type=provider_type,
                models=[env_model],
                api_key=env_key,
                base_url=env_base,
            )
            default_provider = "env"
            default_model = env_model

    return Settings(
        providers=providers,
        default_provider=default_provider,
        default_model=default_model,
        port=int(os.environ.get("PORT", "8000")),
        cwd=resolved_cwd,
        workspace_root=str(workspace_root),
        config_paths=merged.config_paths,
    )


def setup_logging() -> None:
    """Configure default logging."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
