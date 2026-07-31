"""Tests for bundled model metadata lookup behavior."""

from __future__ import annotations

from mycode.models import lookup_model_metadata


def patch_catalog(monkeypatch, catalog: dict[str, object]) -> None:
    monkeypatch.setattr("mycode.models.load_models_catalog", lambda: catalog)


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
            "openrouter": {"moonshotai/kimi-k2.6": {"max_output_tokens": 262_144, "supports_reasoning": True}},
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
