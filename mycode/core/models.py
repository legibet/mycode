"""Load and query the bundled model metadata catalog."""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mycode.core.utils import as_bool, as_int

_MODELS_CATALOG_PATH = Path(__file__).with_name("models_catalog.json")


@dataclass(frozen=True)
class ModelMetadata:
    """Normalized metadata used by provider resolution."""

    provider: str
    model: str
    context_window: int | None
    max_output_tokens: int | None
    supports_reasoning: bool | None
    supports_image_input: bool | None
    supports_pdf_input: bool | None


@functools.cache
def load_models_catalog() -> dict[str, Any] | None:
    """Load the bundled model catalog from disk once per process."""

    try:
        data = json.loads(_MODELS_CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = None
    return data if isinstance(data, dict) else None


def lookup_model_metadata(
    *,
    provider_type: str | None,
    model: str | None,
) -> ModelMetadata | None:
    """Resolve metadata for one internal provider type and model."""

    raw_model_id = (model or "").strip()
    if not raw_model_id:
        return None

    catalog = load_models_catalog()
    if not catalog:
        return None

    normalized_model_id = _strip_prefix(raw_model_id)
    fallback_provider_type = _default_provider(normalized_model_id)

    metadata = _lookup_entry(catalog, provider_type, raw_model_id)
    if metadata is not None:
        return metadata

    if fallback_provider_type and fallback_provider_type != provider_type:
        metadata = _lookup_entry(catalog, fallback_provider_type, normalized_model_id)
        if metadata is not None:
            return metadata

    return _lookup_entry(catalog, "aihubmix", normalized_model_id)


def _lookup_entry(
    catalog: dict[str, Any],
    provider_type: str | None,
    model_id: str,
) -> ModelMetadata | None:
    """Read one exact provider/model entry from the bundled catalog."""

    if not provider_type:
        return None

    provider = catalog.get(provider_type)
    if not isinstance(provider, dict):
        return None

    raw_model = provider.get(model_id)
    if not isinstance(raw_model, dict):
        return None

    return ModelMetadata(
        provider=provider_type,
        model=model_id,
        context_window=as_int(raw_model.get("context_window")),
        max_output_tokens=as_int(raw_model.get("max_output_tokens")),
        supports_reasoning=as_bool(raw_model.get("supports_reasoning")),
        supports_image_input=as_bool(raw_model.get("supports_image_input")),
        supports_pdf_input=as_bool(raw_model.get("supports_pdf_input")),
    )


def _default_provider(model_id: str) -> str | None:
    """Return the canonical internal provider type for a well-known model id."""

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
