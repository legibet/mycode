"""models.dev integration used by provider resolution.

This module stays intentionally small:

- fetch and cache the raw `api.json` payload
- resolve one `(provider, model)` lookup at a time

It does not manage provider config or model selection policy. Those decisions
stay in `config.py`.
"""

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
_FETCH_TIMEOUT_SECONDS = 5.0

_models_dev_cache: dict[str, Any] | None = None
_models_dev_cache_loaded = False


@dataclass(frozen=True)
class ModelMetadata:
    provider: str
    model: str
    name: str | None
    context_window: int | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    supports_reasoning: bool | None
    supports_tools: bool | None
    raw: dict[str, Any]


def load_models_dev(*, force_refresh: bool = False) -> dict[str, Any] | None:
    global _models_dev_cache, _models_dev_cache_loaded

    if not force_refresh and _models_dev_cache_loaded:
        return _models_dev_cache

    cache_path = _models_dev_cache_path()
    data = None if force_refresh else _read_cached_models_dev(cache_path, require_fresh=True)
    if data is None:
        data = _fetch_models_dev()
        if data is not None:
            _write_cached_models_dev(cache_path, data)
        elif not force_refresh:
            data = _read_cached_models_dev(cache_path, require_fresh=False)

    _models_dev_cache = data
    _models_dev_cache_loaded = True
    return data


def lookup_model_metadata(
    *,
    provider_type: str | None,
    provider_name: str | None,
    model: str | None,
    api_base: str | None = None,
) -> ModelMetadata | None:
    model_id = (model or "").strip()
    if not model_id:
        return None

    models_dev = load_models_dev()
    if not models_dev:
        return None

    for candidate_provider in _candidate_provider_ids(
        models_dev,
        provider_type=provider_type,
        provider_name=provider_name,
        model_id=model_id,
        api_base=api_base,
    ):
        metadata = _lookup_provider_model(models_dev, candidate_provider, model_id)
        if metadata is not None:
            return metadata
    return None


def _candidate_provider_ids(
    models_dev: dict[str, Any],
    *,
    provider_type: str | None,
    provider_name: str | None,
    model_id: str,
    api_base: str | None,
) -> list[str]:
    ordered: list[str] = []

    def add(provider_id: str | None) -> None:
        if not provider_id or provider_id in ordered or provider_id not in models_dev:
            return
        ordered.append(provider_id)

    # Prefer the runtime provider id first. After renaming our built-in
    # provider to `moonshotai`, the ids line up with models.dev directly.
    add(provider_type)
    add(provider_name)

    api_host = _host(api_base)
    if api_host:
        for provider_id, provider in models_dev.items():
            if isinstance(provider, dict) and _host(provider.get("api")) == api_host:
                add(provider_id)

    if "/" in model_id:
        add(model_id.split("/", 1)[0])

    exact_matches = _providers_with_model(models_dev, model_id)
    if len(exact_matches) == 1:
        add(exact_matches[0])

    for alias in _model_aliases(model_id)[1:]:
        alias_matches = _providers_with_model(models_dev, alias)
        if len(alias_matches) == 1:
            add(alias_matches[0])

    return ordered


def _lookup_provider_model(models_dev: dict[str, Any], provider_id: str, model_id: str) -> ModelMetadata | None:
    provider = models_dev.get(provider_id)
    if not isinstance(provider, dict):
        return None

    models = provider.get("models")
    if not isinstance(models, dict):
        return None

    for alias in _model_aliases(model_id):
        raw_model = models.get(alias)
        if not isinstance(raw_model, dict):
            continue

        limits = raw_model.get("limit")
        limit_data = limits if isinstance(limits, dict) else {}
        raw_name = raw_model.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        return ModelMetadata(
            provider=provider_id,
            model=str(raw_model.get("id") or alias),
            name=name,
            context_window=limit_data.get("context")
            if isinstance(limit_data.get("context"), int) and not isinstance(limit_data.get("context"), bool)
            else None,
            max_input_tokens=limit_data.get("input")
            if isinstance(limit_data.get("input"), int) and not isinstance(limit_data.get("input"), bool)
            else None,
            max_output_tokens=limit_data.get("output")
            if isinstance(limit_data.get("output"), int) and not isinstance(limit_data.get("output"), bool)
            else None,
            supports_reasoning=raw_model.get("reasoning") if isinstance(raw_model.get("reasoning"), bool) else None,
            supports_tools=raw_model.get("tool_call") if isinstance(raw_model.get("tool_call"), bool) else None,
            raw=raw_model,
        )
    return None


def _providers_with_model(models_dev: dict[str, Any], model_id: str) -> list[str]:
    matches: list[str] = []
    for provider_id, provider in models_dev.items():
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if isinstance(models, dict) and model_id in models:
            matches.append(provider_id)
    return matches


def _model_aliases(model_id: str) -> list[str]:
    aliases = [model_id]
    if "/" in model_id:
        suffix = model_id.split("/", 1)[1].strip()
        if suffix and suffix not in aliases:
            aliases.append(suffix)
    return aliases


def _models_dev_cache_path() -> Path:
    home = os.environ.get("MYCODE_HOME", _DEFAULT_MYCODE_HOME)
    return Path(home).expanduser().resolve(strict=False) / "cache" / "models.dev-api.json"


def _read_cached_models_dev(path: Path, *, require_fresh: bool) -> dict[str, Any] | None:
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


def _write_cached_models_dev(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    except Exception:
        return


def _fetch_models_dev() -> dict[str, Any] | None:
    try:
        with urlopen(_MODELS_DEV_URL, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _host(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    return (parsed.netloc or parsed.path or "").rstrip("/").lower() or None
