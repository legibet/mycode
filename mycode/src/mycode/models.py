"""Load and query the bundled model metadata catalog."""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mycode.utils import as_bool, as_int

_MODELS_CATALOG_PATH = Path(__file__).with_name("models_catalog.json")

# Catalogs consulted only for capability bits (context window, image / pdf
# support, …) when the requested provider has no entry for the model. They
# are NOT registered providers; the metadata returned from a fallback hit is
# always attributed to a real provider type the caller already has in hand.
_FALLBACK_CAPABILITY_CATALOGS: tuple[str, ...] = ("aihubmix",)


@dataclass(frozen=True)
class ModelMetadata:
    """Normalized metadata used by provider resolution."""

    provider: str
    model: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_reasoning: bool | None = None
    supports_image_input: bool | None = None
    supports_pdf_input: bool | None = None


@functools.cache
def load_models_catalog() -> dict[str, Any] | None:
    """Load the bundled model catalog from disk once per process."""

    try:
        data = json.loads(_MODELS_CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = None
    return data if isinstance(data, dict) else None


def infer_provider_from_model(model: str | None) -> str | None:
    """Return the canonical built-in provider id for a known model id, else None.

    Recognizes well-known prefixes on bare model ids and ``provider/model``
    ids alike. Returns ``None`` for unknown ids — callers should require an
    explicit provider in that case rather than guess.
    """

    bare = (model or "").strip().split("/", 1)[-1].strip().lower()
    if bare.startswith("claude-"):
        return "anthropic"
    if bare.startswith("deepseek-"):
        return "deepseek"
    if bare.startswith("gemini-"):
        return "google"
    if bare.startswith("glm-"):
        return "zai"
    if bare.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if bare.startswith("kimi-"):
        return "moonshotai"
    if bare.startswith("minimax-"):
        return "minimax"
    return None


def resolve_model_metadata(
    *,
    provider: str,
    model: str,
    **overrides: Any,
) -> ModelMetadata:
    """Return catalog metadata for ``(provider, model)`` with non-None overrides layered on top.

    Override keys must match :class:`ModelMetadata` fields. Missing overrides
    and an absent catalog entry both leave the corresponding fields at ``None``
    so callers can apply their own fallback defaults.
    """

    base = lookup_model_metadata(provider_type=provider, model=model) or ModelMetadata(provider=provider, model=model)
    return replace(base, **{k: v for k, v in overrides.items() if v is not None})


def lookup_model_metadata(
    *,
    provider_type: str | None,
    model: str | None,
) -> ModelMetadata | None:
    """Resolve metadata for one provider type and model.

    Lookup tiers, in order:

    1. Exact ``(provider_type, model)`` entry.
    2. Canonical provider inferred from the model id prefix (``gpt-`` →
       ``openai``, ``claude-`` → ``anthropic``, …) when the requested
       provider had no hit.
    3. Capability fallback from a secondary catalog (currently
       ``aihubmix``), attributed to the caller's real provider — never
       to the secondary catalog id.
    """

    raw = (model or "").strip()
    if not raw:
        return None
    catalog = load_models_catalog()
    if not catalog:
        return None

    bare = raw.split("/", 1)[1].strip() if "/" in raw else raw
    inferred = infer_provider_from_model(bare)

    if provider_type:
        hit = _match(catalog, lookup=provider_type, model_id=raw, attributed=provider_type)
        if hit is not None:
            return hit

    if inferred and inferred != provider_type:
        hit = _match(catalog, lookup=inferred, model_id=bare, attributed=inferred)
        if hit is not None:
            return hit

    attributed = provider_type or inferred
    if attributed is None:
        return None
    for source in _FALLBACK_CAPABILITY_CATALOGS:
        hit = _match(catalog, lookup=source, model_id=bare, attributed=attributed)
        if hit is not None:
            return hit

    return None


def _match(
    catalog: dict[str, Any],
    *,
    lookup: str,
    model_id: str,
    attributed: str,
) -> ModelMetadata | None:
    """Look up one model in a catalog section and build metadata if present."""

    section = catalog.get(lookup)
    if not isinstance(section, dict):
        return None
    raw = section.get(model_id)
    if not isinstance(raw, dict):
        return None
    return ModelMetadata(
        provider=attributed,
        model=model_id,
        context_window=as_int(raw.get("context_window")),
        max_output_tokens=as_int(raw.get("max_output_tokens")),
        supports_reasoning=as_bool(raw.get("supports_reasoning")),
        supports_image_input=as_bool(raw.get("supports_image_input")),
        supports_pdf_input=as_bool(raw.get("supports_pdf_input")),
    )
