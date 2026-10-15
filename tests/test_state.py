import json
from pathlib import Path

import pytest

from mycode_cli.config import get_settings
from mycode_cli.state import CliState, load_state, resolve_remembered_provider, save_state


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    return tmp_path


def test_state_round_trip(home: Path) -> None:
    state = CliState(provider="openai", model="gpt-5", efforts={"openai/gpt-5": "high"})

    save_state(state)

    assert load_state() == state


def test_state_ignores_invalid_fields(home: Path) -> None:
    (home / "cli.json").write_text('{"provider": 1, "model": "m", "effort": ["invalid"]}', encoding="utf-8")

    assert load_state() == CliState(model="m")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(CliState(provider="beta", model="b2"), ("beta", "b2"), id="remembered"),
        pytest.param(CliState(provider="beta", model="gone"), ("beta", "b1"), id="model-gone"),
        pytest.param(CliState(provider="gone", model="b2"), ("alpha", "a1"), id="provider-gone"),
    ],
)
def test_resolve_remembered_provider_falls_back(
    home: Path, tmp_path: Path, state: CliState, expected: tuple[str, str]
) -> None:
    providers = {
        "alpha": {"type": "openai_chat", "api_key": "k", "models": {"a1": {}}},
        "beta": {"type": "openai_chat", "api_key": "k", "models": {"b1": {}, "b2": {}}},
    }
    (home / "config.json").write_text(json.dumps({"providers": providers}), encoding="utf-8")

    resolved = resolve_remembered_provider(get_settings(str(tmp_path)), state)

    assert (resolved.provider_name, resolved.model) == expected


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        pytest.param(None, "shared", ("beta", "shared"), id="listed-prefers-remembered"),
        pytest.param(None, "a1", ("alpha", "a1"), id="listed-elsewhere"),
        pytest.param(None, "beta/unlisted", ("beta", "unlisted"), id="provider-slash-model"),
        pytest.param(None, "beta/b1", ("alpha", "beta/b1"), id="listed-id-with-slash"),
        pytest.param("beta", "vendor/m", ("beta", "vendor/m"), id="explicit-provider-not-split"),
    ],
)
def test_resolve_remembered_provider_model_reference(
    home: Path, tmp_path: Path, provider: str | None, model: str, expected: tuple[str, str]
) -> None:
    providers = {
        "alpha": {"type": "openai_chat", "api_key": "k", "models": {"a1": {}, "shared": {}, "beta/b1": {}}},
        "beta": {"type": "openai_chat", "api_key": "k", "models": {"b1": {}, "shared": {}}},
    }
    (home / "config.json").write_text(json.dumps({"providers": providers}), encoding="utf-8")
    state = CliState(provider="beta", model="b1")

    resolved = resolve_remembered_provider(get_settings(str(tmp_path)), state, provider_name=provider, model=model)

    assert (resolved.provider_name, resolved.model) == expected


@pytest.mark.parametrize("model", ["unlisted", "beta/", "/b1"])
def test_resolve_remembered_provider_rejects_unknown_model_reference(home: Path, tmp_path: Path, model: str) -> None:
    providers = {"beta": {"type": "openai_chat", "api_key": "k", "models": {"b1": {}}}}
    (home / "config.json").write_text(json.dumps({"providers": providers}), encoding="utf-8")

    with pytest.raises(ValueError, match="provider/model"):
        resolve_remembered_provider(get_settings(str(tmp_path)), CliState(), model=model)
