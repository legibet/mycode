"""Tests for FastAPI app startup behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.routing import Mount
from starlette.testclient import TestClient

from mycode_cli.server.app import create_api_app, create_app


def mount_paths(app) -> list[str]:
    return [route.path for route in app.routes if isinstance(route, Mount)]


@pytest.mark.parametrize(
    ("factory", "has_static", "expected_mounts"),
    [
        (create_app, True, [""]),
        (create_app, False, []),
        (create_api_app, True, []),
    ],
)
def test_web_mount_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory,
    has_static: bool,
    expected_mounts: list[str],
) -> None:
    static_dir = tmp_path if has_static else tmp_path / "missing"
    if has_static:
        (static_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr("mycode_cli.server.app.web_static_path", lambda: static_dir)

    app = factory()

    assert mount_paths(app) == expected_mounts


def test_create_app_starts_without_models_catalog_side_effects() -> None:
    with TestClient(create_app(serve_web=False)):
        pass


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "hi", "input": [{"type": "text", "text": "also hi"}]},
        {"message": "   "},
        {"input": [{"type": "image", "data": "abc"}]},
        {"input": [{"type": "document", "data": "abc", "mime_type": "text/plain"}]},
        {"input": [{"type": "image"}]},
    ],
)
def test_chat_request_shape_validation(payload: dict[str, object]) -> None:
    with TestClient(create_api_app()) as client:
        response = client.post("/api/chat", json=payload)

    assert response.status_code == 422
