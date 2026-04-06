"""Tests for bundled model metadata lookup behavior."""

import mycode.core.models as models
from mycode.core.models import load_models_catalog, lookup_model_metadata


def test_lookup_model_metadata_prefers_current_provider_family(monkeypatch) -> None:
    fake_catalog = {
        "openai": {"gpt-5": {"max_output_tokens": 128_000, "supports_reasoning": True, "supports_image_input": True}},
        "openrouter": {"openai/gpt-5": {"max_output_tokens": 64_000, "supports_reasoning": True}},
    }
    monkeypatch.setattr("mycode.core.models.load_models_catalog", lambda: fake_catalog)

    metadata = lookup_model_metadata(
        provider_type="openrouter",
        model="openai/gpt-5",
    )

    assert metadata is not None
    assert metadata.provider == "openrouter"
    assert metadata.max_output_tokens == 64_000


def test_lookup_model_metadata_falls_back_to_canonical_provider(monkeypatch) -> None:
    fake_catalog = {
        "openai": {"gpt-5": {"max_output_tokens": 128_000, "supports_reasoning": True, "supports_image_input": True}},
        "other": {},
    }
    monkeypatch.setattr("mycode.core.models.load_models_catalog", lambda: fake_catalog)

    metadata = lookup_model_metadata(
        provider_type="openai_chat",
        model="openai/gpt-5",
    )

    assert metadata is not None
    assert metadata.provider == "openai"
    assert metadata.model == "gpt-5"
    assert metadata.supports_image_input is True


def test_lookup_model_metadata_falls_back_to_aihubmix(monkeypatch) -> None:
    fake_catalog = {"aihubmix": {"glm-5.1": {"max_output_tokens": 131_072}}}
    monkeypatch.setattr("mycode.core.models.load_models_catalog", lambda: fake_catalog)

    metadata = lookup_model_metadata(
        provider_type="zai",
        model="glm-5.1",
    )

    assert metadata is not None
    assert metadata.provider == "aihubmix"
    assert metadata.max_output_tokens == 131_072


def test_lookup_model_metadata_does_not_retry_on_miss(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_load_models_catalog():
        calls["count"] += 1
        return {"zai": {}}

    monkeypatch.setattr("mycode.core.models.load_models_catalog", fake_load_models_catalog)

    metadata = lookup_model_metadata(
        provider_type="zai",
        model="glm-5.1",
    )

    assert metadata is None
    assert calls["count"] == 1


def test_load_models_catalog_reads_file_once(monkeypatch, tmp_path) -> None:
    catalog_path = tmp_path / "models_catalog.json"
    catalog_path.write_text('{"openai":{"gpt-5":{}}}', encoding="utf-8")

    monkeypatch.setattr(models, "_MODELS_CATALOG_PATH", catalog_path)
    # Clear the functools.cache so this test gets a fresh read.
    load_models_catalog.cache_clear()

    assert load_models_catalog() == {"openai": {"gpt-5": {}}}
    catalog_path.write_text('{"changed":true}', encoding="utf-8")
    # Second call should return cached result, not re-read.
    assert load_models_catalog() == {"openai": {"gpt-5": {}}}

    load_models_catalog.cache_clear()


def test_lookup_model_metadata_reads_image_support(monkeypatch) -> None:
    fake_catalog = {
        "anthropic": {
            "claude-sonnet-4-6": {
                "max_output_tokens": 64000,
                "supports_reasoning": True,
                "supports_image_input": True,
            }
        }
    }
    monkeypatch.setattr("mycode.core.models.load_models_catalog", lambda: fake_catalog)

    metadata = lookup_model_metadata(provider_type="anthropic", model="claude-sonnet-4-6")

    assert metadata is not None
    assert metadata.supports_image_input is True
