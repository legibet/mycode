"""Tests for bundled model metadata lookup and cost estimation behavior."""

from __future__ import annotations

import json

import pytest

import mycode.models as models
from mycode.models import estimate_cost, lookup_model_metadata


def patch_catalog(monkeypatch, catalog: dict[str, object]) -> None:
    parsed = models._MODELS_CATALOG_ADAPTER.validate_json(json.dumps(catalog))
    monkeypatch.setattr("mycode.models.load_models_catalog", lambda: parsed)


def test_bundled_catalog_is_valid() -> None:
    catalog = models.load_models_catalog()

    assert catalog is not None
    assert catalog


def test_lookup_model_metadata_prefers_provider_specific_match(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "openai": {"gpt-5": {"max_output_tokens": 128_000, "supports_reasoning": True}},
            "openrouter": {
                "openai/gpt-5": {
                    "max_output_tokens": 64_000,
                    "supports_reasoning": True,
                    "supports_image_input": True,
                    "supports_pdf_input": True,
                }
            },
        },
    )

    metadata = lookup_model_metadata(provider_type="openrouter", model="openai/gpt-5")

    assert metadata is not None
    assert metadata.provider == "openrouter"
    assert metadata.max_output_tokens == 64_000
    assert metadata.supports_image_input is True
    assert metadata.supports_pdf_input is True


def test_lookup_model_metadata_falls_back_across_provider_families(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "openai": {
                "gpt-5": {"max_output_tokens": 128_000, "supports_reasoning": True, "supports_image_input": True}
            },
            "openrouter": {
                "moonshotai/kimi-k2.6": {
                    "max_output_tokens": 262_144,
                    "supports_reasoning": True,
                    "reasoning_efforts": ["none", "low", "high"],
                }
            },
        },
    )

    openai_chat = lookup_model_metadata(provider_type="openai_chat", model="openai/gpt-5")
    moonshot = lookup_model_metadata(provider_type="moonshotai", model="kimi-k2.6")

    assert openai_chat is not None
    assert openai_chat.provider == "openai_chat"
    assert openai_chat.model == "openai/gpt-5"
    assert openai_chat.supports_image_input is True

    assert moonshot is not None
    assert moonshot.provider == "moonshotai"
    assert moonshot.model == "kimi-k2.6"
    assert moonshot.max_output_tokens == 262_144
    assert moonshot.supports_reasoning is True
    assert moonshot.reasoning_efforts == ("none", "low", "high")


def test_lookup_model_metadata_rejects_ambiguous_openrouter_suffix_matches(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "openrouter": {
                "first/shared-model": {"max_output_tokens": 64_000},
                "second/shared-model": {"max_output_tokens": 128_000},
            }
        },
    )

    metadata = lookup_model_metadata(provider_type="moonshotai", model="shared-model")

    assert metadata is None


def test_lookup_model_metadata_requires_query_provider(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "openai": {"gpt-5": {"max_output_tokens": 128_000}},
            "openrouter": {"some-provider/some-niche-model": {"max_output_tokens": 64_000}},
        },
    )

    assert lookup_model_metadata(provider_type=None, model="some-niche-model") is None
    assert lookup_model_metadata(provider_type=None, model="gpt-5") is None


def test_cost_comes_from_direct_and_inferred_entries_but_not_suffix_fallback(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "deepseek": {"deepseek-chat": {"context_window": 128_000, "cost": {"input": 0.14, "output": 0.28}}},
            "openrouter": {
                "vendor/niche-model": {"context_window": 64_000, "cost": {"input": 1.0, "output": 2.0}},
            },
        },
    )

    direct = lookup_model_metadata(provider_type="deepseek", model="deepseek-chat")
    assert direct is not None
    assert direct.cost == {"input": 0.14, "output": 0.28}

    # Third-party host of a known model: estimated at official prices.
    inferred = lookup_model_metadata(provider_type="openai_chat", model="deepseek-chat")
    assert inferred is not None
    assert inferred.cost == {"input": 0.14, "output": 0.28}

    # OpenRouter suffix fallback supplies capabilities only, never prices.
    fallback = lookup_model_metadata(provider_type="openai_chat", model="niche-model")
    assert fallback is not None
    assert fallback.context_window == 64_000
    assert fallback.cost is None


def test_catalog_cost_tiers_keep_the_public_json_shape(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "openai": {
                "gpt-5": {
                    "cost": {
                        "input": 1.0,
                        "tiers": [{"size": 200_000, "input": 2.0}],
                    }
                }
            }
        },
    )

    metadata = lookup_model_metadata(provider_type="openai", model="gpt-5")

    assert metadata is not None
    assert metadata.cost == {"input": 1.0, "tiers": [{"size": 200_000, "input": 2.0}]}


# estimate_cost

_SONNET_COST = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}


def test_estimate_cost_bills_a_cached_reasoning_request() -> None:
    usage = {
        "total_tokens": 12_100,
        "input_tokens": 11_200,
        "cache_read_tokens": 10_000,
        "cache_write_tokens": 200,
        "output_tokens": 900,
        "reasoning_tokens": 600,
    }

    # uncached 1000×3 + read 10000×0.3 + write 200×3.75 + output 900×15
    assert estimate_cost(usage, _SONNET_COST) == pytest.approx(0.020_25)


def test_estimate_cost_prefers_the_upstream_reported_cost() -> None:
    assert estimate_cost({"cost_usd": 0.0123, "input_tokens": 10, "output_tokens": 5}, _SONNET_COST) == 0.0123
    # Reported zero (e.g. OpenRouter free models) is a real figure, not "unknown".
    assert estimate_cost({"cost_usd": 0.0}, None) == 0.0


def test_estimate_cost_applies_long_context_tiers_per_request() -> None:
    cost = {
        "input": 1.25,
        "output": 10.0,
        "tiers": [{"size": 200_000, "input": 2.5, "output": 15.0}],
    }

    at_boundary = estimate_cost({"input_tokens": 200_000, "output_tokens": 10}, cost)
    over_boundary = estimate_cost({"input_tokens": 200_001, "output_tokens": 10}, cost)

    assert at_boundary == pytest.approx((200_000 * 1.25 + 10 * 10.0) / 1_000_000)
    assert over_boundary == pytest.approx((200_001 * 2.5 + 10 * 15.0) / 1_000_000)


def test_estimate_cost_bills_distinct_reasoning_price_separately() -> None:
    cost = {"input": 1.0, "output": 10.0, "reasoning": 4.0}
    usage = {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 30}

    assert estimate_cost(usage, cost) == pytest.approx((100 * 1.0 + 20 * 10.0 + 30 * 4.0) / 1_000_000)


@pytest.mark.parametrize(
    ("usage", "cost"),
    [
        # No prices at all.
        pytest.param({"input_tokens": 10, "output_tokens": 5}, None, id="no-prices"),
        # Token totals unknown.
        pytest.param({"output_tokens": 5}, _SONNET_COST, id="input-unknown"),
        # Cache split unknown while cache is priced differently from input.
        pytest.param({"input_tokens": 10, "output_tokens": 5}, _SONNET_COST, id="cache-split-unknown"),
        # Reasoning split unknown while reasoning is priced differently.
        pytest.param(
            {"input_tokens": 10, "output_tokens": 5},
            {"input": 1.0, "output": 10.0, "reasoning": 4.0},
            id="reasoning-split-unknown",
        ),
        # Nonzero category without an applicable price.
        pytest.param(
            {"input_tokens": 10, "cache_read_tokens": 4, "output_tokens": 5},
            {"input": 1.0, "output": 2.0},
            id="unpriced-cache-read",
        ),
        # Inconsistent subsets.
        pytest.param(
            {"input_tokens": 10, "cache_read_tokens": 8, "cache_write_tokens": 8, "output_tokens": 5},
            {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 1.25},
            id="cache-exceeds-input",
        ),
        pytest.param(
            {"input_tokens": 10, "output_tokens": 5, "reasoning_tokens": 9},
            {"input": 1.0, "output": 2.0, "reasoning": 4.0},
            id="reasoning-exceeds-output",
        ),
    ],
)
def test_estimate_cost_returns_none_instead_of_a_wrong_figure(usage, cost) -> None:
    assert estimate_cost(usage, cost) is None


def test_estimate_cost_treats_unreported_cache_as_zero_when_price_matches_input() -> None:
    # No cache prices configured: the split cannot change the figure.
    assert estimate_cost({"input_tokens": 100, "output_tokens": 10}, {"input": 1.0, "output": 2.0}) == pytest.approx(
        (100 * 1.0 + 10 * 2.0) / 1_000_000
    )


def test_estimate_cost_ignores_free_cache_categories_the_upstream_never_reports() -> None:
    # Z.AI-shaped pricing: cache writes are free (0.0) and never counted in the
    # response. Substituting 0 cannot understate, so the estimate proceeds.
    cost = {"input": 1.4, "output": 4.4, "cache_read": 0.26, "cache_write": 0.0}
    usage = {"input_tokens": 100, "cache_read_tokens": 40, "output_tokens": 10}

    assert estimate_cost(usage, cost) == pytest.approx((60 * 1.4 + 40 * 0.26 + 10 * 4.4) / 1_000_000)
