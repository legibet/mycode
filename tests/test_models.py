"""Tests for bundled model metadata lookup and cost estimation behavior."""

from __future__ import annotations

import json

import pytest

import mycode.models as models
from mycode.models import estimate_cost, lookup_model_metadata


def patch_catalog(monkeypatch, catalog: dict[str, object]) -> None:
    parsed = models._ModelsCatalog.model_validate_json(json.dumps(catalog))
    monkeypatch.setattr("mycode.models.load_models_catalog", lambda: parsed)


def test_bundled_catalog_is_valid() -> None:
    catalog = models.load_models_catalog()

    assert catalog is not None
    assert catalog.providers
    assert catalog.fallback


def test_bundled_catalog_uses_meta_metadata_for_muse_fallback() -> None:
    metadata = lookup_model_metadata(provider_type="openai_chat", model="muse-spark-1.2")

    assert metadata is not None
    assert metadata.max_output_tokens == 131_072
    assert metadata.reasoning_efforts == ("minimal", "low", "medium", "high", "xhigh")
    assert metadata.pricing == {"input": 1.25, "output": 4.25, "cache_read": 0.15}


def test_lookup_model_metadata_prefers_provider_specific_match(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "fallback": {"gpt-5": {"max_output_tokens": 128_000, "supports_reasoning": True}},
            "providers": {
                "openrouter": {
                    "openai/gpt-5": {
                        "max_output_tokens": 64_000,
                        "supports_reasoning": True,
                        "supports_image_input": True,
                        "supports_pdf_input": True,
                    }
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


def test_lookup_model_metadata_uses_official_bare_model_fallback(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "fallback": {
                "muse-spark-1.2": {
                    "max_output_tokens": 131_072,
                    "supports_reasoning": True,
                    "reasoning_efforts": ["minimal", "low", "medium", "high", "xhigh"],
                    "cost": {"input": 1.25, "output": 4.25},
                }
            },
            "providers": {},
        },
    )

    metadata = lookup_model_metadata(provider_type="openai_chat", model="meta/muse-spark-1.2")

    assert metadata is not None
    assert metadata.provider == "openai_chat"
    assert metadata.model == "meta/muse-spark-1.2"
    assert metadata.max_output_tokens == 131_072
    assert metadata.reasoning_efforts == ("minimal", "low", "medium", "high", "xhigh")
    assert metadata.pricing == {"input": 1.25, "output": 4.25}


def test_lookup_model_metadata_does_not_scan_openrouter_suffixes(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "fallback": {},
            "providers": {
                "openrouter": {
                    "meta/muse-spark-1.2": {"max_output_tokens": 1_048_576},
                }
            },
        },
    )

    metadata = lookup_model_metadata(provider_type="openai_chat", model="muse-spark-1.2")

    assert metadata is None


def test_lookup_model_metadata_requires_query_provider(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "fallback": {"gpt-5": {"max_output_tokens": 128_000}},
            "providers": {},
        },
    )

    assert lookup_model_metadata(provider_type=None, model="some-niche-model") is None
    assert lookup_model_metadata(provider_type=None, model="gpt-5") is None


def test_pricing_comes_from_exact_and_official_fallback_entries(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "fallback": {
                "deepseek-chat": {
                    "context_window": 128_000,
                    "cost": {"input": 0.14, "output": 0.28},
                }
            },
            "providers": {
                "deepseek": {
                    "deepseek-chat": {
                        "context_window": 64_000,
                        "cost": {"input": 0.2, "output": 0.4},
                    }
                }
            },
        },
    )

    direct = lookup_model_metadata(provider_type="deepseek", model="deepseek-chat")
    assert direct is not None
    assert direct.context_window == 64_000
    assert direct.pricing == {"input": 0.2, "output": 0.4}

    fallback = lookup_model_metadata(provider_type="openai_chat", model="deepseek-chat")
    assert fallback is not None
    assert fallback.context_window == 128_000
    assert fallback.pricing == {"input": 0.14, "output": 0.28}


def test_catalog_pricing_tiers_keep_the_public_json_shape(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "fallback": {},
            "providers": {
                "openai": {
                    "gpt-5": {
                        "cost": {
                            "input": 1.0,
                            "tiers": [{"size": 200_000, "input": 2.0}],
                        }
                    }
                }
            },
        },
    )

    metadata = lookup_model_metadata(provider_type="openai", model="gpt-5")

    assert metadata is not None
    assert metadata.pricing == {"input": 1.0, "tiers": [{"size": 200_000, "input": 2.0}]}


# estimate_cost

_SONNET_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}


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
    assert estimate_cost(usage, _SONNET_PRICING) == pytest.approx(
        {
            "input": 0.003,
            "cache_read": 0.003,
            "cache_write": 0.00075,
            "output": 0.0045,
            "reasoning": 0.009,
            "total": 0.02025,
        }
    )


def test_estimate_cost_keeps_required_zero_components() -> None:
    assert estimate_cost({"input_tokens": 0, "output_tokens": 0}, _SONNET_PRICING) == {
        "input": 0.0,
        "output": 0.0,
        "total": 0.0,
    }


def test_estimate_cost_applies_long_context_tiers_per_request() -> None:
    cost = {
        "input": 1.25,
        "output": 10.0,
        "tiers": [{"size": 200_000, "input": 2.5, "output": 15.0}],
    }

    at_boundary = estimate_cost({"input_tokens": 200_000, "output_tokens": 10}, cost)
    over_boundary = estimate_cost({"input_tokens": 200_001, "output_tokens": 10}, cost)

    assert at_boundary == pytest.approx({"input": 0.25, "output": 0.0001, "total": 0.2501})
    assert over_boundary == pytest.approx({"input": 0.5000025, "output": 0.00015, "total": 0.5001525})


def test_estimate_cost_bills_distinct_reasoning_price_separately() -> None:
    cost = {"input": 1.0, "output": 10.0, "reasoning": 4.0}
    usage = {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 30}

    assert estimate_cost(usage, cost) == pytest.approx(
        {"input": 0.0001, "output": 0.0002, "reasoning": 0.00012, "total": 0.00042}
    )


@pytest.mark.parametrize(
    ("usage", "cost"),
    [
        # No prices at all.
        pytest.param({"input_tokens": 10, "output_tokens": 5}, None, id="no-prices"),
        # Token totals unknown.
        pytest.param({"output_tokens": 5}, _SONNET_PRICING, id="input-unknown"),
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
        pytest.param(
            {"input_tokens": -1, "output_tokens": 5},
            {"input": 1.0, "output": 2.0},
            id="negative-tokens",
        ),
    ],
)
def test_estimate_cost_returns_none_instead_of_a_wrong_figure(usage, cost) -> None:
    assert estimate_cost(usage, cost) is None


def test_estimate_cost_uses_base_prices_for_missing_splits_and_special_prices() -> None:
    cost = {"input": 1.0, "output": 10.0, "cache_read": 0.25, "reasoning": 4.0}

    assert estimate_cost({"input_tokens": 100, "output_tokens": 10}, cost) == pytest.approx(
        {"input": 0.0001, "output": 0.0001, "total": 0.0002}
    )
    assert estimate_cost(
        {
            "input_tokens": 100,
            "cache_read_tokens": 40,
            "cache_write_tokens": 10,
            "output_tokens": 10,
            "reasoning_tokens": 4,
        },
        cost,
    ) == pytest.approx(
        {
            "input": 0.00005,
            "cache_read": 0.00001,
            "cache_write": 0.00001,
            "output": 0.00006,
            "reasoning": 0.000016,
            "total": 0.000146,
        }
    )


def test_estimate_cost_ignores_free_cache_categories_the_upstream_never_reports() -> None:
    # Z.AI-shaped pricing: cache writes are free (0.0) and never counted in the
    # response. Substituting 0 cannot understate, so the estimate proceeds.
    cost = {"input": 1.4, "output": 4.4, "cache_read": 0.26, "cache_write": 0.0}
    usage = {"input_tokens": 100, "cache_read_tokens": 40, "output_tokens": 10}

    assert estimate_cost(usage, cost) == pytest.approx(
        {"input": 0.000084, "cache_read": 0.0000104, "output": 0.000044, "total": 0.0001384}
    )
