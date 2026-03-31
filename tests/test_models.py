"""Tests for models.dev metadata lookup behavior."""

import mycode.core.models as models
from mycode.core.models import initialize_models_dev, lookup_model_metadata


def test_lookup_model_metadata_prefers_current_provider_family(monkeypatch) -> None:
    fake_catalog = {
        "openai": {
            "models": {"gpt-5": {"id": "gpt-5", "reasoning": True, "tool_call": True, "limit": {"output": 128_000}}}
        },
        "openrouter": {
            "models": {
                "openai/gpt-5": {
                    "id": "openai/gpt-5",
                    "reasoning": True,
                    "tool_call": True,
                    "limit": {"output": 64_000},
                }
            }
        },
    }
    monkeypatch.setattr("mycode.core.models.load_models_dev", lambda: fake_catalog)

    metadata = lookup_model_metadata(
        provider_type="openrouter",
        provider_name="router",
        model="openai/gpt-5",
        api_base="https://openrouter.ai/api/v1",
    )

    assert metadata is not None
    assert metadata.provider == "openrouter"
    assert metadata.max_output_tokens == 64_000


def test_lookup_model_metadata_falls_back_to_canonical_provider(monkeypatch) -> None:
    fake_catalog = {
        "openai": {
            "models": {"gpt-5": {"id": "gpt-5", "reasoning": True, "tool_call": True, "limit": {"output": 128_000}}}
        },
        "other": {"models": {}},
    }
    monkeypatch.setattr("mycode.core.models.load_models_dev", lambda: fake_catalog)

    metadata = lookup_model_metadata(
        provider_type="openai_chat",
        provider_name="compat",
        model="openai/gpt-5",
        api_base="https://proxy.example/v1",
    )

    assert metadata is not None
    assert metadata.provider == "openai"
    assert metadata.model == "gpt-5"


def test_lookup_model_metadata_falls_back_to_aihubmix(monkeypatch) -> None:
    fake_catalog = {"aihubmix": {"models": {"glm-5.1": {"id": "glm-5.1", "limit": {"output": 131_072}}}}}
    monkeypatch.setattr("mycode.core.models.load_models_dev", lambda: fake_catalog)

    metadata = lookup_model_metadata(
        provider_type="zai",
        provider_name="zhipu-coding",
        model="glm-5.1",
        api_base="https://open.bigmodel.cn/api/coding/paas/v4",
    )

    assert metadata is not None
    assert metadata.provider == "aihubmix"
    assert metadata.max_output_tokens == 131_072


def test_lookup_model_metadata_does_not_retry_on_miss(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_load_models_dev():
        calls["count"] += 1
        return {"zai": {"models": {}}}

    monkeypatch.setattr("mycode.core.models.load_models_dev", fake_load_models_dev)

    metadata = lookup_model_metadata(
        provider_type="zai",
        provider_name="zhipu-coding",
        model="glm-5.1",
        api_base="https://open.bigmodel.cn/api/coding/paas/v4",
    )

    assert metadata is None
    assert calls["count"] == 1


def test_initialize_models_dev_fetches_once_at_startup(monkeypatch) -> None:
    cached_catalog = {"cached": {"models": {}}}
    fresh_catalog = {"fresh": {"models": {}}}
    writes: list[dict] = []
    fetch_calls = {"count": 0}

    monkeypatch.setattr(models, "_models_dev_cache", None)
    monkeypatch.setattr(models, "_models_dev_cache_loaded", False)
    monkeypatch.setattr("mycode.core.models._read_cached_models_dev", lambda _path: cached_catalog)
    monkeypatch.setattr("mycode.core.models._write_cached_models_dev", lambda _path, data: writes.append(data))

    def fake_fetch_models_dev():
        fetch_calls["count"] += 1
        return fresh_catalog

    monkeypatch.setattr("mycode.core.models._fetch_models_dev", fake_fetch_models_dev)

    assert initialize_models_dev() == fresh_catalog
    assert initialize_models_dev() == fresh_catalog
    assert fetch_calls["count"] == 1
    assert writes == [fresh_catalog]
