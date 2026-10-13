"""Tests for bundled model metadata lookup."""

from __future__ import annotations

import json

import mycode.models as models
from mycode.models import lookup_model_metadata


def patch_catalog(monkeypatch, catalog: dict[str, object]) -> None:
    parsed = models._ModelsCatalog.model_validate_json(json.dumps(catalog))
    monkeypatch.setattr("mycode.models.load_models_catalog", lambda: parsed)


def test_bundled_catalog_is_valid() -> None:
    catalog = models.load_models_catalog()

    assert catalog is not None
    assert catalog.models


def test_bundled_catalog_uses_official_meta_metadata() -> None:
    metadata = lookup_model_metadata(provider_type="openai_chat", model="muse-spark-1.2")

    assert metadata is not None
    assert metadata.max_output_tokens == 131_072
    assert metadata.reasoning_efforts == ("minimal", "low", "medium", "high", "xhigh")
    assert metadata.pricing == {"input": 1.25, "output": 4.25, "cache_read": 0.15}


def test_lookup_model_metadata_uses_official_model_name(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "models": {
                "muse-spark-1.2": {
                    "max_output_tokens": 131_072,
                    "reasoning_efforts": ["minimal", "low", "medium", "high", "xhigh"],
                    "cost": {"input": 1.25, "output": 4.25},
                }
            },
        },
    )

    metadata = lookup_model_metadata(provider_type="openai_chat", model="meta/muse-spark-1.2")

    assert metadata is not None
    assert metadata.provider == "openai_chat"
    assert metadata.model == "meta/muse-spark-1.2"
    assert metadata.max_output_tokens == 131_072
    assert metadata.reasoning_efforts == ("minimal", "low", "medium", "high", "xhigh")
    assert metadata.pricing == {"input": 1.25, "output": 4.25}


def test_lookup_model_metadata_requires_query_provider(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "models": {"gpt-5": {"max_output_tokens": 128_000}},
        },
    )

    assert lookup_model_metadata(provider_type=None, model="some-niche-model") is None
    assert lookup_model_metadata(provider_type=None, model="gpt-5") is None


def test_pricing_comes_from_official_model_for_any_provider(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "models": {
                "deepseek-chat": {
                    "context_window": 128_000,
                    "cost": {"input": 0.14, "output": 0.28},
                }
            },
        },
    )

    for provider in ("deepseek", "openai_chat", "openrouter"):
        for model in ("deepseek-chat", "deepseek/deepseek-chat"):
            metadata = lookup_model_metadata(provider_type=provider, model=model)
            assert metadata is not None
            assert metadata.provider == provider
            assert metadata.model == model
            assert metadata.context_window == 128_000
            assert metadata.pricing == {"input": 0.14, "output": 0.28}


def test_catalog_pricing_tiers_keep_the_public_json_shape(monkeypatch) -> None:
    patch_catalog(
        monkeypatch,
        {
            "models": {
                "gpt-5": {
                    "cost": {
                        "input": 1.0,
                        "tiers": [{"size": 200_000, "input": 2.0}],
                    }
                }
            },
        },
    )

    metadata = lookup_model_metadata(provider_type="openai", model="gpt-5")

    assert metadata is not None
    assert metadata.pricing == {"input": 1.0, "tiers": [{"size": 200_000, "input": 2.0}]}
