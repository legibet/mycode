"""Tests for the /api/settings endpoints.

Scope: only behaviours that are easy to break and not covered elsewhere —
secret masking, the three-state ``api_key`` PUT semantics, and the
settings write-normalisation edge cases that aren't already tested via
``get_settings``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mycode.providers import list_env_discoverable_providers, provider_env_api_key_names
from mycode_cli.server.app import create_api_app


@pytest.fixture(autouse=True)
def clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for provider in list_env_discoverable_providers():
        for env_name in provider_env_api_key_names(provider):
            monkeypatch.delenv(env_name, raising=False)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "home" / ".mycode"
    monkeypatch.setenv("MYCODE_HOME", str(target))
    return target


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_api_app())


def _write_config(home: Path, payload: dict[str, object]) -> None:
    path = home / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class TestSettingsApi:
    def test_get_returns_empty_when_no_file(self, client: TestClient, home: Path) -> None:
        body = client.get("/api/settings").json()
        assert body["exists"] is False
        assert body["config"] == {}

    def test_get_masks_secret_and_preserves_env_ref(
        self, client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "set")
        _write_config(
            home,
            {
                "providers": {
                    "anthropic": {"type": "anthropic", "api_key": "sk-secret"},
                    "router": {"type": "openrouter", "api_key": "${MY_CUSTOM_KEY}"},
                }
            },
        )

        body = client.get("/api/settings").json()
        anthropic = body["config"]["providers"]["anthropic"]
        router = body["config"]["providers"]["router"]
        env = body["env"]

        assert anthropic["api_key"] is None
        assert anthropic["api_key_saved"] is True
        assert router["api_key"] == "${MY_CUSTOM_KEY}"
        assert router["api_key_saved"] is False
        assert env["ANTHROPIC_API_KEY"] is True
        assert env["MY_CUSTOM_KEY"] is False

    @pytest.mark.parametrize(
        ("api_key", "expected"),
        [
            (None, "sk-old"),  # null → keep existing secret
            ("", None),  # empty string → clear
            ("sk-new", "sk-new"),  # string → replace
        ],
    )
    def test_put_api_key_three_states(
        self, client: TestClient, home: Path, api_key: str | None, expected: str | None
    ) -> None:
        _write_config(home, {"providers": {"anthropic": {"type": "anthropic", "api_key": "sk-old"}}})

        response = client.put(
            "/api/settings",
            json={"config": {"providers": {"anthropic": {"type": "anthropic", "api_key": api_key}}}},
        )
        assert response.status_code == 200, response.text

        on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert on_disk["providers"]["anthropic"].get("api_key") == expected

    def test_put_round_trip_writes_models_as_dict(self, client: TestClient, home: Path) -> None:
        response = client.put(
            "/api/settings",
            json={
                "config": {
                    "default": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                    "providers": {
                        "anthropic": {
                            "type": "anthropic",
                            "models": ["claude-sonnet-4-6"],
                            "api_key": "sk",
                        }
                    },
                }
            },
        )
        assert response.status_code == 200

        on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
        # UI sends models as a list; storage normalises to the dict form expected
        # by the rest of the codebase (so model-metadata overrides keep working).
        assert on_disk["providers"]["anthropic"]["models"] == {"claude-sonnet-4-6": {}}

    def test_put_returns_400_on_invalid_input(self, client: TestClient, home: Path) -> None:
        response = client.put(
            "/api/settings",
            json={"config": {"providers": {"weird": {"type": "not-a-real-provider"}}}},
        )
        assert response.status_code == 400
        assert "unsupported" in response.json()["detail"]


class TestSettingsWriteNormalization:
    """Edge cases that the round-trip API tests don't directly exercise."""

    def test_put_drops_empty_fields(self, client: TestClient, home: Path) -> None:
        response = client.put(
            "/api/settings",
            json={
                "config": {
                    "default": {"provider": "", "model": None},
                    "permission": None,
                    "providers": {"anthropic": {"api_key": "", "base_url": None, "models": []}},
                }
            },
        )
        assert response.status_code == 200

        on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert on_disk == {"providers": {"anthropic": {}}}

    def test_put_compact_threshold_false_persists_as_false(self, client: TestClient, home: Path) -> None:
        response = client.put("/api/settings", json={"config": {"default": {"compact_threshold": False}}})
        assert response.status_code == 200

        on_disk = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert on_disk == {"default": {"compact_threshold": False}}
