"""Load and query the bundled model metadata catalog."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from mycode.messages import USAGE_TOKEN_KEYS

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
    supports_reasoning: bool | None = None
    reasoning_efforts: tuple[str, ...] | None = None
    supports_image_input: bool | None = None
    supports_pdf_input: bool | None = None
    cost: _ModelCost | None = None


type _ModelsCatalog = dict[str, dict[str, _CatalogEntry]]

_MODELS_CATALOG_ADAPTER = TypeAdapter(_ModelsCatalog)


@dataclass(frozen=True)
class ModelMetadata:
    """Model metadata for the requested provider/model.

    ``provider`` and ``model`` keep the original query identity. Other fields
    may come from a fallback catalog entry.

    ``pricing`` holds models.dev prices in USD per 1M tokens — keys ``input``,
    ``output``, ``cache_read``, ``cache_write``, ``reasoning`` plus optional
    ``tiers`` (long-context price overrides, ``[{"size": ..., <prices>}]``).
    Capability fields may come from the OpenRouter suffix fallback, but
    ``pricing`` never does: prices apply only when the catalog entry belongs to
    the requested provider or the inferred official provider.

    ``reasoning_efforts`` is ``None`` when the source has no reasoning options,
    an empty tuple when it advertises no effort option, or the advertised
    string effort values.
    """

    provider: str
    model: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_reasoning: bool | None = None
    reasoning_efforts: tuple[str, ...] | None = None
    supports_image_input: bool | None = None
    supports_pdf_input: bool | None = None
    pricing: dict[str, Any] | None = None


class Cost(TypedDict):
    """USD cost for one provider request or a request aggregate."""

    total: float
    input: NotRequired[float]
    cache_read: NotRequired[float]
    cache_write: NotRequired[float]
    output: NotRequired[float]
    reasoning: NotRequired[float]


@cache
def load_models_catalog() -> _ModelsCatalog | None:
    """Load the bundled model catalog from disk once per process."""

    try:
        return _MODELS_CATALOG_ADAPTER.validate_json(_MODELS_CATALOG_PATH.read_bytes())
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
    supports_reasoning: bool | None = None,
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
        "supports_reasoning": supports_reasoning,
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

    Catalog lookup order:

    1. Requested provider and requested model.
    2. Inferred provider and unprefixed model name.
    3. Unique OpenRouter model whose suffix matches the model name.
    """

    requested_model = (model or "").strip()
    if not provider_type or not requested_model:
        return None
    catalog = load_models_catalog()
    if not catalog:
        return None

    model_name = requested_model.split("/", 1)[1].strip() if "/" in requested_model else requested_model
    catalog_entry = catalog.get(provider_type, {}).get(requested_model)

    inferred_provider = infer_provider_from_model(model_name)
    if catalog_entry is None and inferred_provider and inferred_provider != provider_type:
        catalog_entry = catalog.get(inferred_provider, {}).get(model_name)

    from_suffix_fallback = False
    if catalog_entry is None:
        catalog_entry = _get_openrouter_suffix_entry(catalog, model_name)
        from_suffix_fallback = catalog_entry is not None

    if catalog_entry is None:
        return None

    return ModelMetadata(
        provider=provider_type,
        model=requested_model,
        context_window=catalog_entry.context_window,
        max_output_tokens=catalog_entry.max_output_tokens,
        supports_reasoning=catalog_entry.supports_reasoning,
        reasoning_efforts=catalog_entry.reasoning_efforts,
        supports_image_input=catalog_entry.supports_image_input,
        supports_pdf_input=catalog_entry.supports_pdf_input,
        pricing=(
            catalog_entry.cost.model_dump(
                mode="json",
                exclude_none=True,
                exclude_defaults=True,
            )
            if catalog_entry.cost is not None and not from_suffix_fallback
            else None
        ),
    )


_PRICE_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")


def estimate_cost(usage: dict[str, Any], pricing: dict[str, Any] | None) -> Cost | None:
    """Estimate the USD cost of one provider request.

    ``usage`` is a message's canonical ``meta.usage`` dict and ``pricing`` is
    :attr:`ModelMetadata.pricing`. Returns ``None`` when the available
    token/price data cannot produce a complete estimate. Missing cache and
    reasoning prices use the corresponding base input/output rate.
    """

    if not pricing:
        return None

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    if any(usage.get(key, 0) < 0 for key in USAGE_TOKEN_KEYS):
        return None

    prices: dict[str, float | None] = {key: pricing.get(key) for key in _PRICE_KEYS}
    # Long-context pricing: the highest tier the full input (incl. cache)
    # exceeds overrides the base prices; fields missing on a tier inherit.
    for tier in pricing.get("tiers") or []:
        if input_tokens > tier["size"]:
            prices.update({key: tier[key] for key in _PRICE_KEYS if tier.get(key) is not None})

    cache_read = usage.get("cache_read_tokens", 0)
    cache_write = usage.get("cache_write_tokens", 0)

    uncached_input = input_tokens - cache_read - cache_write
    if uncached_input < 0:
        return None

    reasoning = usage.get("reasoning_tokens", 0)
    if reasoning > output_tokens:
        return None
    cache_read_price = prices["cache_read"] if prices["cache_read"] is not None else prices["input"]
    cache_write_price = prices["cache_write"] if prices["cache_write"] is not None else prices["input"]
    reasoning_price = prices["reasoning"] if prices["reasoning"] is not None else prices["output"]

    component_data = (
        (uncached_input, prices["input"]),
        (cache_read, cache_read_price),
        (cache_write, cache_write_price),
        (output_tokens - reasoning, prices["output"]),
        (reasoning, reasoning_price),
    )
    if any(tokens and price is None for tokens, price in component_data):
        return None

    input_cost = uncached_input * (prices["input"] or 0.0) / 1_000_000
    output_cost = (output_tokens - reasoning) * (prices["output"] or 0.0) / 1_000_000
    cost: Cost = {
        "total": input_cost + output_cost,
        "input": input_cost,
        "output": output_cost,
    }
    if cache_read:
        cost["cache_read"] = cache_read * (cache_read_price or 0.0) / 1_000_000
        cost["total"] += cost["cache_read"]
    if cache_write:
        cost["cache_write"] = cache_write * (cache_write_price or 0.0) / 1_000_000
        cost["total"] += cost["cache_write"]
    if reasoning:
        cost["reasoning"] = reasoning * (reasoning_price or 0.0) / 1_000_000
        cost["total"] += cost["reasoning"]
    return cost


def _get_openrouter_suffix_entry(catalog: _ModelsCatalog, model_name: str) -> _CatalogEntry | None:
    """Return an OpenRouter entry with a unique matching model suffix."""

    openrouter = catalog.get("openrouter", {})

    match: _CatalogEntry | None = None
    for model_id, catalog_entry in openrouter.items():
        if "/" not in model_id:
            continue
        if model_id.split("/", 1)[1].strip() != model_name:
            continue
        if match is not None:
            return None
        match = catalog_entry
    return match
