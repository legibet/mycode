"""Tests for config loading."""

from __future__ import annotations

from pathlib import Path

from mycode.core.config import get_settings, resolve_provider


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestGetSettings:
    def test_merges_global_and_project_configs(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        project = tmp_path / "project"
        cwd = project / "apps" / "api"
        cwd.mkdir(parents=True)
        (project / ".git").mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "providers": {
                "shared": {
                  "type": "openai",
                  "api_key": "global-key",
                  "models": ["gpt-5-mini"]
                }
              },
              "default": {
                "provider": "shared",
                "model": "gpt-5-mini"
              }
            }
            """,
        )
        _write(
            project / ".mycode" / "config.json",
            """
            {
              "default": {
                "provider": "shared",
                "model": "gpt-5.4"
              },
              "providers": {
                "shared": {
                  "base_url": "https://root.example/v1",
                  "models": ["gpt-5.4"]
                }
              }
            }
            """,
        )

        settings = get_settings(str(cwd))

        assert settings.cwd == str(cwd.resolve())
        assert settings.workspace_root == str(project.resolve())
        assert settings.default_provider == "shared"
        assert settings.default_model == "gpt-5.4"
        assert settings.providers["shared"].api_key == "global-key"
        assert settings.providers["shared"].base_url == "https://root.example/v1"
        assert settings.providers["shared"].models == ["gpt-5.4"]
        assert settings.config_paths == [
            str((home / ".mycode" / "config.json").resolve()),
            str((project / ".mycode" / "config.json").resolve()),
        ]

    def test_ignores_model_and_base_url_env_without_config(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("MODEL", "openai:gpt-5.4")
        monkeypatch.setenv("BASE_URL", "https://env.example/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")

        settings = get_settings(str(workspace))

        assert settings.providers == {}
        assert settings.default_provider is None
        assert settings.default_model is None

    def test_resolve_provider_prefers_env_api_key_over_config(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        monkeypatch.setenv("BASE_URL", "https://env.example/v1")

        _write(
            home / ".mycode" / "config.json",
            """
            {
                "providers": {
                  "shared": {
                    "type": "anthropic",
                    "api_key": "config-key",
                    "base_url": "https://config.example/v1",
                    "models": ["claude-sonnet-4-6"]
                  }
                },
                "default": {
                  "provider": "shared",
                  "model": "claude-sonnet-4-6"
                }
              }
            """,
        )

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings)

        assert resolved.provider_type == "anthropic"
        assert resolved.model == "claude-sonnet-4-6"
        assert resolved.api_key == "env-key"
        assert resolved.api_base == "https://config.example/v1"

    def test_resolve_provider_accepts_raw_supported_provider(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-env-key")

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings, provider_name="moonshot", model="kimi-k2-thinking")

        assert resolved.provider_type == "moonshot"
        assert resolved.model == "kimi-k2-thinking"
        assert resolved.api_key == "moonshot-env-key"

    def test_resolve_provider_prefers_explicit_api_key_over_env(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "providers": {
                "claude": {
                  "type": "anthropic",
                  "api_key": "config-key",
                  "models": ["claude-sonnet-4-6"]
                }
              },
              "default": {
                "provider": "claude",
                "model": "claude-sonnet-4-6"
              }
            }
            """,
        )

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings, api_key="request-key")

        assert resolved.api_key == "request-key"

    def test_ignores_agents_config(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.delenv("MODEL", raising=False)
        monkeypatch.delenv("BASE_URL", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        _write(
            home / ".agents" / "config.json",
            """
            {
              "default": {
                "provider": "compat"
              },
              "providers": {
                "compat": {
                  "type": "openai",
                  "models": ["gpt-5.4"]
                }
              }
            }
            """,
        )

        settings = get_settings(str(workspace))

        assert settings.providers == {}
        assert settings.default_provider is None
        assert settings.config_paths == []
