"""Thin models.dev lookup helpers for model metadata."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

_DEFAULT_MYCODE_HOME = "~/.mycode"
_MODELS_DEV_URL = "https://models.dev/api.json"
_CACHE_TTL_SECONDS = 60 * 60 * 24
_DEFAULT_FETCH_TIMEOUT = 5.0

_BUILTIN_PROVIDER_MODEL_DEFAULTS: dict[str, list[str]] = {
    "anthropic": ["claude-sonnet-4-6", "claude-opus-4-1"],
    "moonshot": ["kimi-k2.5"],
    "minimax": ["MiniMax-M2.7", "MiniMax-M2.7-highspeed"],
    "openai": ["gpt-5.4", "gpt-5.4-mini"],
}

_CATALOG_PROVIDER_BY_RUNTIME_PROVIDER: dict[str, tuple[str, ...]] = {
    "anthropic": ("anthropic",),
    "moonshot": ("moonshotai",),
    "minimax": ("minimax",),
    "openai": ("openai",),
}

_catalog_cache: dict[str, Any] | None = None
_catalog_cache_loaded = False


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    name: str | None
    context_window: int | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    supports_reasoning: bool | None
    supports_tools: bool | None
    raw: dict[str, Any]


def default_models_for_provider(provider_type: str | None) -> list[str]:
    if not provider_type:
        return []
    return list(_BUILTIN_PROVIDER_MODEL_DEFAULTS.get(provider_type, ()))


def lookup_model_spec(
    *,
    provider_type: str | None,
    provider_name: str | None,
    model: str | None,
    api_base: str | None = None,
) -> ModelSpec | None:
    model_id = (model or "").strip()
    if not model_id:
        return None

    catalog = load_model_catalog()
    if not catalog:
        return None

    provider_ids = _candidate_provider_ids(
        catalog,
        provider_type=provider_type,
        provider_name=provider_name,
        model=model_id,
        api_base=api_base,
    )
    for provider_id in provider_ids:
        spec = _lookup_in_provider(catalog, provider_id, model_id)
        if spec is not None:
            return spec
    return None


def load_model_catalog(*, force_refresh: bool = False) -> dict[str, Any] | None:
    global _catalog_cache, _catalog_cache_loaded

    if not force_refresh and _catalog_cache_loaded:
        return _catalog_cache

    cache_path = _catalog_cache_path()
    catalog = None if force_refresh else _read_cached_catalog(cache_path, require_fresh=True)
    if catalog is None:
        catalog = _fetch_catalog()
        if catalog is not None:
            _write_cached_catalog(cache_path, catalog)
        elif not force_refresh:
            catalog = _read_cached_catalog(cache_path, require_fresh=False)

    _catalog_cache = catalog
    _catalog_cache_loaded = True
    return catalog


def _lookup_in_provider(catalog: dict[str, Any], provider_id: str, model_id: str) -> ModelSpec | None:
    provider = catalog.get(provider_id)
    if not isinstance(provider, dict):
        return None

    models = provider.get("models")
    if not isinstance(models, dict):
        return None

    for candidate in _candidate_model_ids(model_id):
        raw_model = models.get(candidate)
        if not isinstance(raw_model, dict):
            continue
        raw_limits = raw_model.get("limit")
        limits: dict[str, Any] = raw_limits if isinstance(raw_limits, dict) else {}
        return ModelSpec(
            provider=provider_id,
            model=str(raw_model.get("id") or candidate),
            name=_as_optional_str(raw_model.get("name")),
            context_window=_as_optional_int(limits.get("context")),
            max_input_tokens=_as_optional_int(limits.get("input")),
            max_output_tokens=_as_optional_int(limits.get("output")),
            supports_reasoning=_as_optional_bool(raw_model.get("reasoning")),
            supports_tools=_as_optional_bool(raw_model.get("tool_call")),
            raw=raw_model,
        )
    return None


def _candidate_provider_ids(
    catalog: dict[str, Any],
    *,
    provider_type: str | None,
    provider_name: str | None,
    model: str,
    api_base: str | None,
) -> list[str]:
    ordered: list[str] = []

    def add(value: str | None) -> None:
        if not value or value in ordered or value not in catalog:
            return
        ordered.append(value)

    for provider_id in _CATALOG_PROVIDER_BY_RUNTIME_PROVIDER.get(provider_type or "", ()):
        add(provider_id)

    add(provider_name)

    for provider_id in _provider_ids_for_api_base(catalog, api_base):
        add(provider_id)

    if "/" in model:
        add(model.split("/", 1)[0])

    for provider_id in _unique_global_provider_ids(catalog, model):
        add(provider_id)

    return ordered


def _candidate_model_ids(model_id: str) -> list[str]:
    candidates = [model_id]
    if "/" in model_id:
        suffix = model_id.split("/", 1)[1]
        if suffix and suffix not in candidates:
            candidates.append(suffix)
    return candidates


def _provider_ids_for_api_base(catalog: dict[str, Any], api_base: str | None) -> list[str]:
    base_host = _api_host(api_base)
    if not base_host:
        return []

    matches: list[str] = []
    for provider_id, raw in catalog.items():
        if not isinstance(raw, dict):
            continue
        if _api_host(raw.get("api")) == base_host:
            matches.append(provider_id)
    return matches


def _unique_global_provider_ids(catalog: dict[str, Any], model_id: str) -> list[str]:
    matches = []
    for provider_id, raw in catalog.items():
        if not isinstance(raw, dict):
            continue
        models = raw.get("models")
        if isinstance(models, dict) and model_id in models:
            matches.append(provider_id)
    if len(matches) == 1:
        return matches

    if "/" not in model_id:
        return []

    suffix = model_id.split("/", 1)[1]
    matches = []
    for provider_id, raw in catalog.items():
        if not isinstance(raw, dict):
            continue
        models = raw.get("models")
        if isinstance(models, dict) and suffix in models:
            matches.append(provider_id)
    return matches if len(matches) == 1 else []


def _catalog_cache_path() -> Path:
    home = os.environ.get("MYCODE_HOME", _DEFAULT_MYCODE_HOME)
    return Path(home).expanduser().resolve(strict=False) / "cache" / "models.dev-api.json"


def _read_cached_catalog(path: Path, *, require_fresh: bool) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        if require_fresh:
            age_seconds = max(0.0, time.time() - path.stat().st_mtime)
            if age_seconds > _CACHE_TTL_SECONDS:
                return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_cached_catalog(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    except Exception:
        return


def _fetch_catalog() -> dict[str, Any] | None:
    try:
        with urlopen(_MODELS_DEV_URL, timeout=_DEFAULT_FETCH_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _api_host(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    return (parsed.netloc or parsed.path or "").rstrip("/").lower() or None


def _as_optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_optional_bool(value: Any) -> bool | None:
    if not isinstance(value, bool):
        return None
    return value


def _as_optional_str(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
