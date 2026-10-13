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
def load_models_catalog() -> dict[str, _CatalogEntry]:
    """Load the bundled catalog, keyed by official model name, once per process."""

    try:
        return _ModelsCatalog.model_validate_json(_MODELS_CATALOG_PATH.read_bytes()).models
    except (OSError, ValidationError):
        return {}


def infer_provider_from_model(model: str) -> str | None:
    """Return the canonical built-in provider id for a known model id, else None."""

    bare = model.split("/", 1)[-1].strip().lower()
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

    The catalog is keyed by official model name. ``model`` is looked up as
    supplied and then without its ``owner/`` prefix, so routed ids such as
    OpenRouter's ``owner/model`` resolve to the official entry (with the
    official provider's pricing). Fields that neither the catalog nor an
    override supplies stay ``None`` so callers can apply their own defaults.
    """

    metadata = ModelMetadata(provider=provider, model=model)
    catalog = load_models_catalog()
    entry = catalog.get(model) or catalog.get(model.split("/", 1)[-1])
    if entry is not None:
        metadata = replace(
            metadata,
            context_window=entry.context_window,
            max_output_tokens=entry.max_output_tokens,
            reasoning_efforts=entry.reasoning_efforts,
            supports_image_input=entry.supports_image_input,
            supports_pdf_input=entry.supports_pdf_input,
            pricing=entry.cost.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            if entry.cost is not None
            else None,
        )
    overrides = {
        "context_window": context_window,
        "max_output_tokens": max_output_tokens,
        "reasoning_efforts": reasoning_efforts,
        "supports_image_input": supports_image_input,
        "supports_pdf_input": supports_pdf_input,
    }
    return replace(metadata, **{k: v for k, v in overrides.items() if v is not None})
