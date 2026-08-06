"""Load and query the bundled model metadata catalog."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

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

    ``cost`` holds models.dev prices in USD per 1M tokens — keys ``input``,
    ``output``, ``cache_read``, ``cache_write``, ``reasoning`` plus optional
    ``tiers`` (long-context price overrides, ``[{"size": ..., <prices>}]``).
    Capability fields may come from the OpenRouter suffix fallback, but
    ``cost`` never does: prices apply only when the catalog entry belongs to
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
    cost: dict[str, Any] | None = None


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
        cost=(
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


def estimate_cost(usage: dict[str, Any], cost: dict[str, Any] | None) -> float | None:
    """Estimate the USD cost of one provider request.

    ``usage`` is a message's canonical ``meta.usage`` dict; ``cost`` is
    :attr:`ModelMetadata.cost`. Returns the upstream-reported cost when
    present, otherwise a models.dev-based estimate — or None when the
    available token/price data cannot produce a trustworthy figure. There are
    no partial results: a priced category with unknown tokens, an unpriced
    category with nonzero tokens, or inconsistent subsets all yield None
    rather than a silently wrong number.
    """

    reported = usage.get("cost_usd")
    if reported is not None:
        return float(reported)
    if not cost:
        return None

    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None or output_tokens is None:
        return None

    prices: dict[str, float | None] = {key: cost.get(key) for key in _PRICE_KEYS}
    # Long-context pricing: the highest tier the full input (incl. cache)
    # exceeds overrides the base prices; fields missing on a tier inherit.
    for tier in cost.get("tiers") or []:
        if input_tokens > tier["size"]:
            prices.update({key: tier[key] for key in _PRICE_KEYS if tier.get(key) is not None})

    # A cache category the upstream didn't report counts as 0 — unless it has
    # a nonzero price different from plain input, making the split
    # load-bearing. A free (0-priced) category substituted with 0 can only
    # bill those tokens at the input rate, never understate.
    cache_read = usage.get("cache_read_tokens")
    if cache_read is None:
        if prices["cache_read"] and prices["cache_read"] != prices["input"]:
            return None
        cache_read = 0
    cache_write = usage.get("cache_write_tokens")
    if cache_write is None:
        if prices["cache_write"] and prices["cache_write"] != prices["input"]:
            return None
        cache_write = 0

    uncached_input = input_tokens - cache_read - cache_write
    if uncached_input < 0:
        return None

    reasoning = usage.get("reasoning_tokens")
    if reasoning is not None and reasoning > output_tokens:
        return None
    reasoning_price = prices["reasoning"] if prices["reasoning"] is not None else prices["output"]
    if reasoning_price == prices["output"]:
        # Same effective rate — bill the full output without needing the split.
        reasoning = 0
    elif reasoning is None:
        return None

    total = 0.0
    for tokens, price in (
        (uncached_input, prices["input"]),
        (cache_read, prices["cache_read"]),
        (cache_write, prices["cache_write"]),
        (output_tokens - reasoning, prices["output"]),
        (reasoning, reasoning_price),
    ):
        if not tokens:
            continue
        if price is None:
            return None
        total += tokens * price
    return total / 1_000_000


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
