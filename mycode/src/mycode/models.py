"""Load and query the bundled model metadata catalog."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

_MODELS_CATALOG_PATH = Path(__file__).with_name("models_catalog.json")


class _CatalogSchema(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class _CostTier(_CatalogSchema):
    size: int
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None
    reasoning: float | None = None


class _ModelCost(_CatalogSchema):
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None
    reasoning: float | None = None
    tiers: tuple[_CostTier, ...] = ()


class _CatalogEntry(_CatalogSchema):
    context_window: int | None = None
    max_output_tokens: int | None = None
    reasoning_efforts: tuple[str, ...] | None = None
    supports_image_input: bool | None = None
    supports_pdf_input: bool | None = None
    cost: _ModelCost | None = None


class _ModelsCatalog(_CatalogSchema):
    models: dict[str, _CatalogEntry]


@dataclass(frozen=True)
class ModelMetadata:
    """Model metadata for the requested provider/model.

    ``provider`` and ``model`` keep the original query identity. Other fields
    come from the official model entry.

    ``pricing`` holds models.dev prices in USD per 1M tokens — keys ``input``,
    ``output``, ``cache_read``, ``cache_write``, ``reasoning`` plus optional
    ``tiers`` (long-context price overrides, ``[{"size": ..., <prices>}]``).
    Official metadata comes from the model owner's selected official endpoint,
    including its pricing and reasoning efforts.

    ``reasoning_efforts`` is ``None`` when the source has no reasoning options,
    an empty tuple when it advertises no effort option, or the advertised
    string effort values.
    """

    provider: str
    model: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    reasoning_efforts: tuple[str, ...] | None = None
    supports_image_input: bool | None = None
    supports_pdf_input: bool | None = None
    pricing: dict[str, Any] | None = None


@cache
def load_models_catalog() -> _ModelsCatalog | None:
    """Load the bundled model catalog from disk once per process."""

    try:
        return _ModelsCatalog.model_validate_json(_MODELS_CATALOG_PATH.read_bytes())
    except (OSError, ValidationError):
        return None


def infer_provider_from_model(model: str | None) -> str | None:
    """Return the canonical built-in provider id for a known model id, else None."""

    bare = (model or "").strip().split("/", 1)[-1].strip().lower()
    if bare.startswith("claude-"):
        return "anthropic"
    if bare.startswith("deepseek-"):
        return "deepseek"
    if bare.startswith("gemini-"):
        return "google"
    if bare.startswith("glm-"):
        return "zai"
    if bare.startswith("grok-"):
        return "xai"
    if bare.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if bare.startswith("kimi-"):
        return "moonshotai"
    if bare.startswith("minimax-"):
        return "minimax"
    if bare.startswith(("qwen", "qvq-", "qwq-")):
        return "alibaba"
    return None


def resolve_model_metadata(
    *,
    provider: str,
    model: str,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
    reasoning_efforts: tuple[str, ...] | None = None,
    supports_image_input: bool | None = None,
    supports_pdf_input: bool | None = None,
) -> ModelMetadata:
    """Return catalog metadata for ``(provider, model)`` with non-None overrides layered on top.

    Missing overrides and an absent catalog entry both leave the corresponding
    fields at ``None`` so callers can apply their own fallback defaults.
    """

    base = lookup_model_metadata(provider_type=provider, model=model) or ModelMetadata(provider=provider, model=model)
    overrides = {
        "context_window": context_window,
        "max_output_tokens": max_output_tokens,
        "reasoning_efforts": reasoning_efforts,
        "supports_image_input": supports_image_input,
        "supports_pdf_input": supports_pdf_input,
    }
    return replace(base, **{k: v for k, v in overrides.items() if v is not None})


def lookup_model_metadata(
    *,
    provider_type: str | None,
    model: str | None,
) -> ModelMetadata | None:
    """Return model metadata for a provider/model request.

    Looks up the official model name, first as supplied and then without its
    prefix, so routed ids such as OpenRouter's ``owner/model`` resolve to the
    official entry (with the official provider's pricing).
    """

    requested_model = (model or "").strip()
    if not provider_type or not requested_model:
        return None
    catalog = load_models_catalog()
    if catalog is None:
        return None

    model_name = requested_model.split("/", 1)[-1].strip()
    catalog_entry = catalog.models.get(requested_model) or catalog.models.get(model_name)
    if catalog_entry is None:
        return None

    return ModelMetadata(
        provider=provider_type,
        model=requested_model,
        context_window=catalog_entry.context_window,
        max_output_tokens=catalog_entry.max_output_tokens,
        reasoning_efforts=catalog_entry.reasoning_efforts,
        supports_image_input=catalog_entry.supports_image_input,
        supports_pdf_input=catalog_entry.supports_pdf_input,
        pricing=(
            catalog_entry.cost.model_dump(
                mode="json",
                exclude_none=True,
                exclude_defaults=True,
            )
            if catalog_entry.cost is not None
            else None
        ),
    )
