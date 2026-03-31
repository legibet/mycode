"""Fetch and query models.dev metadata."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

_DEFAULT_MYCODE_HOME = "~/.mycode"
_MODELS_DEV_URL = "https://models.dev/api.json"
_FETCH_TIMEOUT_SECONDS = 5.0

_models_dev_cache: dict[str, Any] | None = None
_models_dev_cache_loaded = False


@dataclass(frozen=True)
class ModelMetadata:
    """Normalized metadata used by provider resolution."""

    provider: str
    model: str
    context_window: int | None
    max_output_tokens: int | None
    supports_reasoning: bool | None


def initialize_models_dev() -> dict[str, Any] | None:
    """Refresh the catalog once when the process starts, then keep it in memory."""

    global _models_dev_cache, _models_dev_cache_loaded

    if _models_dev_cache_loaded:
        return _models_dev_cache

    cache_path = _models_dev_cache_path()
    data = _read_cached_models_dev(cache_path)
    if data is None:
        data = _fetch_models_dev()
        if data is not None:
            _write_cached_models_dev(cache_path, data)
    else:
        fetched = _fetch_models_dev()
        if fetched is not None:
            _write_cached_models_dev(cache_path, fetched)
            data = fetched

    _models_dev_cache = data
    _models_dev_cache_loaded = True
    return data


def load_models_dev() -> dict[str, Any] | None:
    """Load the raw models.dev catalog from memory or local cache only."""

    global _models_dev_cache, _models_dev_cache_loaded

    if _models_dev_cache_loaded:
        return _models_dev_cache

    data = _read_cached_models_dev(_models_dev_cache_path())
    _models_dev_cache = data
    _models_dev_cache_loaded = True
    return data


def lookup_model_metadata(
    *,
    provider_type: str | None,
    model: str | None,
    provider_name: str | None = None,  # reserved for future per-alias lookup
    api_base: str | None = None,  # reserved for future custom-endpoint lookup
) -> ModelMetadata | None:
    """Resolve metadata for one internal provider type and model."""

    raw_model_id = (model or "").strip()
    if not raw_model_id:
        return None

    catalog = load_models_dev()
    if not catalog:
        return None

    normalized_model_id = _strip_prefix(raw_model_id)
    fallback_provider_type = _default_provider(normalized_model_id)

    # The current provider must match the exact model id it uses at runtime.
    metadata = _lookup_entry(catalog, provider_type, raw_model_id)
    if metadata is not None:
        return metadata

    # The fallback provider owns the canonical model family, so use the
    # normalized model id without any provider prefix.
    if fallback_provider_type and fallback_provider_type != provider_type:
        metadata = _lookup_entry(catalog, fallback_provider_type, normalized_model_id)
        if metadata is not None:
            return metadata

    # aihubmix keeps a broad catalog of canonical model ids.
    metadata = _lookup_entry(catalog, "aihubmix", normalized_model_id)
    if metadata is not None:
        return metadata

    return None


def _lookup_entry(
    catalog: dict[str, Any],
    provider_type: str | None,
    model_id: str,
) -> ModelMetadata | None:
    """Read one exact provider/model entry from the catalog."""

    if not provider_type:
        return None

    provider = catalog.get(provider_type)
    if not isinstance(provider, dict):
        return None

    models = provider.get("models")
    if not isinstance(models, dict):
        return None

    raw_model = models.get(model_id)
    if not isinstance(raw_model, dict):
        return None

    limits = raw_model.get("limit")
    limit_data = limits if isinstance(limits, dict) else {}
    return ModelMetadata(
        provider=provider_type,
        model=str(raw_model.get("id") or model_id),
        context_window=_as_int(limit_data.get("context")),
        max_output_tokens=_as_int(limit_data.get("output")),
        supports_reasoning=raw_model.get("reasoning") if isinstance(raw_model.get("reasoning"), bool) else None,
    )


def _default_provider(model_id: str) -> str | None:
    """Return the canonical internal provider type for a normalized model id."""

    normalized = model_id.lower()
    if normalized.startswith("claude-"):
        return "anthropic"
    if normalized.startswith("deepseek-"):
        return "deepseek"
    if normalized.startswith("gemini-"):
        return "google"
    if normalized.startswith("glm-"):
        return "zai"
    if normalized.startswith("gpt-") or normalized.startswith(("o1", "o3", "o4")):
        return "openai"
    if normalized.startswith("kimi-"):
        return "moonshotai"
    if normalized.startswith("minimax-"):
        return "minimax"
    return None


def _strip_prefix(model_id: str) -> str:
    """Convert `provider/model` ids into the bare model id."""

    if "/" not in model_id:
        return model_id
    return model_id.split("/", 1)[1].strip()


def _as_int(value: Any) -> int | None:
    """Return an int value while rejecting bools and non-ints."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _models_dev_cache_path() -> Path:
    home = os.environ.get("MYCODE_HOME", _DEFAULT_MYCODE_HOME)
    return Path(home).expanduser().resolve(strict=False) / "cache" / "models.dev-api.json"


def _read_cached_models_dev(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
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
        request = Request(_MODELS_DEV_URL, headers={"User-Agent": "mycode/1.0"})
        with urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None
