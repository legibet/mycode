"""Token usage and USD cost for provider requests.

Each assistant message carries one request's canonical ``meta.usage`` and
``meta.cost``; the functions here build those values and fold them into
running totals (see docs/sessions.md for the usage semantics).
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class Usage(TypedDict, total=False):
    """Token counts for one provider request or a request aggregate.

    A missing key means the upstream did not report it; readers must not
    substitute 0. See :func:`build_usage` for the field semantics.
    """

    total_tokens: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int


USAGE_TOKEN_KEYS = (
    "total_tokens",
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
)

_PRICE_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")


class Cost(TypedDict):
    """USD cost for one provider request or a request aggregate."""

    total: float
    input: NotRequired[float]
    cache_read: NotRequired[float]
    cache_write: NotRequired[float]
    output: NotRequired[float]
    reasoning: NotRequired[float]


def build_usage(
    *,
    total_tokens: int | None = None,
    input_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
) -> Usage:
    """Build a canonical usage dict from one provider request.

    ``input_tokens`` is the full effective input including cache reads and
    writes; ``output_tokens`` includes reasoning; ``cache_*_tokens`` and
    ``reasoning_tokens`` are subsets of their respective totals. A missing key
    means the upstream did not report it — readers must not substitute 0.
    """

    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    usage: Usage = {}
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if cache_read_tokens is not None:
        usage["cache_read_tokens"] = cache_read_tokens
    if cache_write_tokens is not None:
        usage["cache_write_tokens"] = cache_write_tokens
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    if reasoning_tokens is not None:
        usage["reasoning_tokens"] = reasoning_tokens
    return usage


def add_usage(total: Usage, usage: Usage) -> Usage:
    """Return ``total`` plus one request's token counts; missing fields keep known totals."""

    result = total.copy()
    for key in USAGE_TOKEN_KEYS:
        value = usage.get(key)
        if value is not None:
            result[key] = result.get(key, 0) + value
    return result


def estimate_cost(usage: Usage, pricing: dict[str, Any] | None) -> Cost | None:
    """Estimate the USD cost of one provider request.

    ``usage`` is a message's canonical ``meta.usage`` dict and ``pricing`` is
    :attr:`mycode.models.ModelMetadata.pricing`. Returns ``None`` when the
    available token/price data cannot produce a complete estimate. Missing
    cache and reasoning prices use the corresponding base input/output rate.
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


def add_cost(total: Cost | None, cost: Cost | None) -> Cost | None:
    """Return ``total`` plus one request's cost.

    The breakdown survives only while every summed cost carries one; a
    total-only cost (e.g. OpenRouter's reported charge) reduces the sum to
    ``{"total": ...}``.
    """

    if cost is None:
        return total
    if total is None:
        return cost

    summed = total["total"] + cost["total"]
    if not all("input" in item and "output" in item for item in (total, cost)):
        return {"total": summed}

    result: Cost = {
        "total": summed,
        "input": total.get("input", 0.0) + cost.get("input", 0.0),
        "output": total.get("output", 0.0) + cost.get("output", 0.0),
    }
    if "cache_read" in total or "cache_read" in cost:
        result["cache_read"] = total.get("cache_read", 0.0) + cost.get("cache_read", 0.0)
    if "cache_write" in total or "cache_write" in cost:
        result["cache_write"] = total.get("cache_write", 0.0) + cost.get("cache_write", 0.0)
    if "reasoning" in total or "reasoning" in cost:
        result["reasoning"] = total.get("reasoning", 0.0) + cost.get("reasoning", 0.0)
    return result
