"""Tests for models.dev metadata lookup behavior."""

from mycode.core.models import lookup_model_metadata


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
    monkeypatch.setattr("mycode.core.models.load_models_dev", lambda **_: fake_catalog)

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
    monkeypatch.setattr("mycode.core.models.load_models_dev", lambda **_: fake_catalog)

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
    monkeypatch.setattr("mycode.core.models.load_models_dev", lambda **_: fake_catalog)

    metadata = lookup_model_metadata(
        provider_type="zai",
        provider_name="zhipu-coding",
        model="glm-5.1",
        api_base="https://open.bigmodel.cn/api/coding/paas/v4",
    )

    assert metadata is not None
    assert metadata.provider == "aihubmix"
    assert metadata.max_output_tokens == 131_072


def test_lookup_model_metadata_refreshes_once_on_miss(monkeypatch) -> None:
    stale_catalog = {"zai": {"models": {}}}
    fresh_catalog = {"zai": {"models": {"glm-5.1": {"id": "glm-5.1", "limit": {"output": 131_072}}}}}
    calls: list[bool] = []

    def fake_load_models_dev(*, force_refresh: bool = False):
        calls.append(force_refresh)
        return fresh_catalog if force_refresh else stale_catalog

    monkeypatch.setattr("mycode.core.models.load_models_dev", fake_load_models_dev)

    metadata = lookup_model_metadata(
        provider_type="zai",
        provider_name="zhipu-coding",
        model="glm-5.1",
        api_base="https://open.bigmodel.cn/api/coding/paas/v4",
    )

    assert metadata is not None
    assert metadata.provider == "zai"
    assert calls == [False, True]
