"""Configuration document validation, settings loading, and provider resolution."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mycode.models import resolve_model_metadata
from mycode.providers import (
    get_provider_adapter,
    is_supported_provider,
    list_env_discoverable_providers,
    list_supported_providers,
    provider_api_key_from_env,
    provider_default_models,
    provider_env_api_key_names,
)
from mycode.utils import as_bool, as_int

_DEFAULT_MYCODE_HOME = "~/.mycode"
_API_KEY_ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# "auto" means "do not send an explicit effort to the provider"; after
# normalization it is represented as None.
REASONING_EFFORT_OPTIONS = ("auto", "none", "low", "medium", "high", "xhigh")
PERMISSION_LEVEL_OPTIONS = ("readonly", "safe", "standard", "yolo")
PERMISSION_MODE_OPTIONS = ("ask", "deny")
MODEL_OVERRIDE_KEYS = (
    "context_window",
    "max_output_tokens",
    "supports_reasoning",
    "supports_image_input",
    "supports_pdf_input",
)
_EFFORT_AUTO_ALIASES = frozenset({"", "auto", "default"})
_EFFORT_OFF_ALIASES = frozenset({"off", "disabled"})

PermissionLevel = Literal["readonly", "safe", "standard", "yolo"]
PermissionMode = Literal["ask", "deny"]


@dataclass(frozen=True)
class ModelConfig:
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_reasoning: bool | None = None
    supports_image_input: bool | None = None
    supports_pdf_input: bool | None = None


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
class PermissionConfig:
    level: PermissionLevel = "safe"
    mode: PermissionMode = "ask"


@dataclass(frozen=True)
class Settings:
    providers: dict[str, ProviderConfig]
    default_provider: str | None
    default_model: str | None
    port: int
    cwd: str
    project: str
    default_reasoning_effort: str | None = None
    compact_threshold: float | None = None
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    config_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedProvider:
    """Resolved provider ready for Agent construction."""

    provider: str
    model: str
    api_key: str | None
    api_base: str | None
    reasoning_effort: str | None
    provider_name: str | None = None
    model_config: ModelConfig | None = None


def resolve_mycode_home() -> Path:
    """Resolve the mycode home directory (``$MYCODE_HOME`` or ``~/.mycode``)."""

    raw = os.environ.get("MYCODE_HOME", _DEFAULT_MYCODE_HOME)
    return Path(raw).expanduser().resolve(strict=False)


def resolve_sessions_dir() -> Path:
    """Resolve the directory used for persisted sessions."""

    return resolve_mycode_home() / "sessions"


def resolve_project(cwd: str) -> Path:
    """Return the nearest Git project root, or cwd when no .git is found."""

    cwd_path = Path(cwd).expanduser().resolve(strict=False)
    for path in (cwd_path, *cwd_path.parents):
        if (path / ".git").exists():
            return path
    return cwd_path


def project_dirs(cwd: str, project: str | Path | None = None) -> list[Path]:
    """Return directories from project to cwd, inclusive."""

    cwd_path = Path(cwd).expanduser().resolve(strict=False)
    if project is None:
        project_path = resolve_project(cwd)
    else:
        project_path = Path(project).expanduser().resolve(strict=False)

    dirs = [cwd_path]
    while dirs[-1] != project_path:
        parent = dirs[-1].parent
        if parent == dirs[-1]:
            break
        dirs.append(parent)

    return list(reversed(dirs))


def _candidate_config_paths(cwd: str, project: str | Path) -> list[Path]:
    """Return config files in override order: global, then project to cwd."""

    paths = [resolve_mycode_home() / "config.json"]
    paths.extend(path / ".mycode" / "config.json" for path in project_dirs(cwd, project))
    return paths


def is_api_key_env_ref(value: str) -> str | None:
    """Return the env var name when ``value`` is a ``${NAME}`` reference."""

    match = _API_KEY_ENV_REF_RE.fullmatch(value.strip())
    return match.group(1) if match else None


def parse_compact_threshold(value: Any) -> float | None:
    """Parse compact_threshold from config."""

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
    """Normalize a reasoning effort setting."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"reasoning_effort must be a string, got {type(value).__name__}")

    effort = value.strip().lower()
    if effort in _EFFORT_AUTO_ALIASES:
        return None
    if effort in _EFFORT_OFF_ALIASES:
        return "none"
    if effort in REASONING_EFFORT_OPTIONS:
        return effort
    supported = ", ".join(REASONING_EFFORT_OPTIONS)
    raise ValueError(f"unsupported reasoning_effort {value!r}; supported: {supported}")


def normalize_permission_level(value: Any) -> PermissionLevel:
    if not isinstance(value, str):
        raise ValueError(f"permission level must be a string, got {type(value).__name__}")
    level = value.strip().lower()
    if level in PERMISSION_LEVEL_OPTIONS:
        return level
    supported = ", ".join(PERMISSION_LEVEL_OPTIONS)
    raise ValueError(f"unsupported permission level {value!r}; supported: {supported}")


def normalize_permission_mode(value: Any) -> PermissionMode:
    if not isinstance(value, str):
        raise ValueError(f"permission mode must be a string, got {type(value).__name__}")
    mode = value.strip().lower()
    if mode in PERMISSION_MODE_OPTIONS:
        return mode
    supported = ", ".join(PERMISSION_MODE_OPTIONS)
    raise ValueError(f"unsupported permission mode {value!r}; supported: {supported}")


def parse_permission(value: Any, current: PermissionConfig | None = None) -> PermissionConfig:
    base = current or PermissionConfig()
    if value is None:
        return base
    if isinstance(value, str):
        return PermissionConfig(level=normalize_permission_level(value), mode=base.mode)
    if isinstance(value, dict):
        level = normalize_permission_level(value.get("level")) if "level" in value else base.level
        mode = normalize_permission_mode(value.get("mode")) if "mode" in value else base.mode
        return PermissionConfig(level=level, mode=mode)
    raise ValueError(f"permission must be a string or object, got {type(value).__name__}")


def validate_global_config(data: Any) -> dict[str, Any]:
    """Validate and clean a global config document before writing it to disk."""

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("config must be an object")

    out: dict[str, Any] = {}

    if data.get("default") is not None:
        default = _validate_default_config(data["default"])
        if default:
            out["default"] = default

    if data.get("permission") is not None:
        out["permission"] = _validate_permission_config(data["permission"])

    raw_providers = data.get("providers")
    if raw_providers is not None:
        if not isinstance(raw_providers, dict):
            raise ValueError("providers must be an object")
        providers: dict[str, dict[str, Any]] = {}
        for name, raw in raw_providers.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("provider name must be a non-empty string")
            provider_name = name.strip()
            providers[provider_name] = _validate_provider_config(provider_name, raw)
        if providers:
            out["providers"] = providers

    return out


def _optional_config_string(raw: dict[str, Any], key: str, label: str) -> str | None:
    if key not in raw:
        return None
    value = raw[key]
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value.strip() or None


def _validate_default_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("default must be an object")

    out: dict[str, Any] = {}
    for key in ("provider", "model"):
        value = _optional_config_string(raw, key, f"default.{key}")
        if value:
            out[key] = value

    effort = raw.get("reasoning_effort")
    if effort not in (None, ""):
        normalize_reasoning_effort(effort)
        out["reasoning_effort"] = effort

    compact_threshold = raw.get("compact_threshold")
    if compact_threshold is False:
        out["compact_threshold"] = False
    elif compact_threshold is not None:
        if isinstance(compact_threshold, bool):
            raise ValueError("default.compact_threshold must be a number in [0, 1] or false")
        parsed = parse_compact_threshold(compact_threshold)
        if parsed is None:
            raise ValueError("default.compact_threshold must be a number in [0, 1] or false")
        out["compact_threshold"] = parsed

    return out


def _validate_permission_config(raw: Any) -> PermissionLevel | dict[str, Any]:
    if isinstance(raw, str):
        return normalize_permission_level(raw)
    if not isinstance(raw, dict):
        raise ValueError("permission must be a string or object")

    out: dict[str, Any] = {}
    if "level" in raw:
        out["level"] = normalize_permission_level(raw.get("level"))
    if "mode" in raw:
        out["mode"] = normalize_permission_mode(raw.get("mode"))
    return out


def _validate_provider_config(name: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"provider {name!r} must be an object")

    out: dict[str, Any] = {}
    raw_type = raw.get("type")
    if raw_type in (None, ""):
        if not is_supported_provider(name):
            raise ValueError(f"provider {name!r} must set 'type'")
    elif not isinstance(raw_type, str):
        raise ValueError(f"provider {name!r}: type must be a string")
    elif not is_supported_provider(raw_type):
        supported = ", ".join(list_supported_providers())
        raise ValueError(f"provider {name!r}: unsupported type {raw_type!r}; supported: {supported}")
    else:
        out["type"] = raw_type

    for key in ("api_key", "base_url"):
        value = _optional_config_string(raw, key, f"provider {name!r}: {key}")
        if value:
            out[key] = value

    effort = raw.get("reasoning_effort")
    if effort not in (None, ""):
        normalize_reasoning_effort(effort)
        out["reasoning_effort"] = effort

    raw_models = raw.get("models")
    if raw_models is not None:
        models = _validate_model_config_entries(name, raw_models)
        if models:
            out["models"] = models

    return out


def _validate_model_config_entries(name: str, raw: Any) -> dict[str, dict[str, Any]]:
    # The settings UI sends a model list, while stored config uses a model map.
    if isinstance(raw, list):
        items: list[tuple[Any, Any]] = [(model, None) for model in raw]
    elif isinstance(raw, dict):
        items = list(raw.items())
    else:
        raise ValueError(f"provider {name!r}: models must be a list or object")

    out: dict[str, dict[str, Any]] = {}
    for model_id, overrides in items:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError(f"provider {name!r}: model id must be a non-empty string")
        key = model_id.strip()
        if overrides is None:
            out[key] = {}
        elif isinstance(overrides, dict):
            out[key] = {k: v for k, v in overrides.items() if k in MODEL_OVERRIDE_KEYS and v is not None}
        else:
            raise ValueError(f"provider {name!r}: model {key!r} config must be an object")
    return out


def get_settings(cwd: str | None = None) -> Settings:
    """Load settings from global and project config files."""

    resolved_cwd = str(Path(cwd or os.getcwd()).expanduser().resolve(strict=False))
    resolved_project = str(resolve_project(resolved_cwd))

    raw_providers: dict[str, dict[str, Any]] = {}
    default_provider: str | None = None
    default_model: str | None = None
    default_reasoning_effort: str | None = None
    compact_threshold: float | None = None
    permission = PermissionConfig()
    config_paths: list[str] = []

    for path in _candidate_config_paths(resolved_cwd, resolved_project):
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

        default = data.get("default")
        if isinstance(default, dict):
            if "provider" in default:
                v = default.get("provider")
                default_provider = v if isinstance(v, str) else None
            if "model" in default:
                v = default.get("model")
                default_model = v if isinstance(v, str) else None
            if "reasoning_effort" in default:
                v = default.get("reasoning_effort")
                default_reasoning_effort = v if isinstance(v, str) else None
            if "compact_threshold" in default:
                parsed_threshold = parse_compact_threshold(default.get("compact_threshold"))
                if parsed_threshold is not None:
                    compact_threshold = parsed_threshold

        if "permission" in data:
            permission = parse_permission(data.get("permission"), permission)

    return Settings(
        providers=_build_providers(raw_providers),
        default_provider=default_provider,
        default_model=default_model,
        default_reasoning_effort=normalize_reasoning_effort(default_reasoning_effort),
        compact_threshold=compact_threshold,
        permission=permission,
        port=int(os.environ.get("PORT", "8000")),
        cwd=resolved_cwd,
        project=resolved_project,
        config_paths=config_paths,
    )


def _load_json(path: Path) -> dict[str, Any] | None:
    """Load a JSON object from a config file; ignore missing or invalid files."""

    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _normalize_models(value: Any) -> dict[str, ModelConfig]:
    """Convert raw model overrides into runtime model config objects."""

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
        models[model_id] = ModelConfig(
            context_window=as_int(raw_config.get("context_window")),
            max_output_tokens=as_int(raw_config.get("max_output_tokens")),
            supports_reasoning=as_bool(raw_config.get("supports_reasoning")),
            supports_image_input=as_bool(raw_config.get("supports_image_input")),
            supports_pdf_input=as_bool(raw_config.get("supports_pdf_input")),
        )
    return models


def _parse_config_api_key(value: Any) -> tuple[str | None, str | None]:
    """Return a literal API key or an env var reference from config."""

    if not isinstance(value, str):
        return None, None

    cleaned = value.strip()
    if not cleaned:
        return None, None

    env_name = is_api_key_env_ref(cleaned)
    if env_name:
        return None, env_name
    return cleaned, None


def _build_providers(raw_providers: dict[str, dict[str, Any]]) -> dict[str, ProviderConfig]:
    """Build runtime provider configs after layered config files are merged."""

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


def provider_has_api_key(provider: ProviderConfig) -> bool:
    """Return whether a configured provider can authenticate right now."""

    if provider.api_key_env_var:
        return bool(_config_api_key_from_env_var(provider))
    return bool(provider.api_key or provider_api_key_from_env(provider.type))


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
        try:
            return _resolve_provider_runtime(
                settings,
                selected_name=selected_name,
                model=model,
                api_key=api_key,
                api_base=api_base,
            )
        except ValueError:
            # A caller-supplied provider_name must fail loudly rather than silently fall back.
            if provider_name:
                raise

    refs = _available_provider_references(settings)
    if refs:
        return _resolve_provider_runtime(
            settings,
            selected_name=refs[0][0],
            model=model,
            api_key=api_key,
            api_base=api_base,
        )

    env_names = list(
        dict.fromkeys(
            env_name
            for provider_id in list_env_discoverable_providers()
            for env_name in provider_env_api_key_names(provider_id)
        )
    )
    checked = ", ".join(env_names) or "<api key env>"
    raise ValueError(
        "no available providers found; set one of the supported API key env vars "
        + f"({checked}) or configure a provider in ~/.mycode/config.json or a project .mycode/config.json"
    )


def resolve_provider_choices(settings: Settings) -> list[ResolvedProvider]:
    """Return currently selectable providers in stable selection order."""

    choices: list[ResolvedProvider] = []
    for selected_name, _ in _available_provider_references(settings):
        try:
            choices.append(_resolve_provider_runtime(settings, selected_name=selected_name))
        except ValueError:
            continue
    return choices


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

    configured_effort = (
        provider_config.reasoning_effort
        if provider_config and provider_config.reasoning_effort is not None
        else settings.default_reasoning_effort
    )

    # Drop reasoning_effort when the model does not support reasoning. Config
    # overrides win over catalog metadata.
    model_config = provider_config.models.get(resolved_model) if provider_config else None
    meta = resolve_model_metadata(
        provider=provider_type,
        model=resolved_model,
        supports_reasoning=model_config.supports_reasoning if model_config else None,
    )
    adapter = get_provider_adapter(provider_type)
    reasoning_effort = (
        configured_effort
        if configured_effort is not None and meta.supports_reasoning is True and adapter.supports_reasoning_effort
        else None
    )

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
        model_config=model_config,
    )
