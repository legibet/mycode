"""Tests for FastAPI app startup behavior."""

from __future__ import annotations

from starlette.routing import Mount
from starlette.testclient import TestClient

from mycode.server.app import create_app


def _mount_paths(app) -> list[str]:
    return [route.path for route in app.routes if isinstance(route, Mount)]


def test_create_app_mounts_frontend_when_static_exists(tmp_path, monkeypatch) -> None:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr("mycode.server.app.frontend_static_path", lambda: tmp_path)

    app = create_app()

    assert "" in _mount_paths(app)


def test_create_app_skips_frontend_mount_when_static_missing(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "static"
    monkeypatch.setattr("mycode.server.app.frontend_static_path", lambda: missing)

    app = create_app()

    assert _mount_paths(app) == []


def test_create_app_skips_frontend_mount_in_dev_mode(tmp_path, monkeypatch) -> None:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr("mycode.server.app.frontend_static_path", lambda: tmp_path)

    app = create_app(serve_frontend=False)

    assert _mount_paths(app) == []


def test_create_app_initializes_models_catalog_on_startup(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_initialize_models_dev():
        calls["count"] += 1
        return None

    monkeypatch.setattr("mycode.server.app.initialize_models_dev", fake_initialize_models_dev)

    with TestClient(create_app(serve_frontend=False)):
        pass

    assert calls["count"] == 1
