"""Tests for config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycode.core.config import get_settings, resolve_provider
from mycode.core.models import ModelMetadata, lookup_model_metadata


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def _disable_live_models_dev_lookup(monkeypatch) -> None:
    monkeypatch.setattr("mycode.core.config.lookup_model_metadata", lambda **_: None)


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch) -> None:
    for env_name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY",
        "DEEPSEEK_API_KEY",
        "ZAI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(env_name, raising=False)


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
        resolved = resolve_provider(settings, provider_name="moonshotai", model="kimi-k2-thinking")

        assert resolved.provider_type == "moonshotai"
        assert resolved.model == "kimi-k2-thinking"
        assert resolved.api_key == "moonshot-env-key"

    def test_resolve_provider_accepts_raw_google_provider(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-env-key")

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings, provider_name="google")

        assert resolved.provider_type == "google"
        assert resolved.model == "gemini-3.1-pro-preview"
        assert resolved.api_key == "gemini-env-key"

    def test_resolve_provider_auto_discovers_first_available_provider(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("OPENAI_API_KEY", "openai-env-key")
        monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-env-key")

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings)

        assert resolved.provider_name == "openai"
        assert resolved.provider_type == "openai"
        assert resolved.model == "gpt-5.4"
        assert resolved.api_key == "openai-env-key"

    def test_resolve_provider_prefers_first_configured_provider_with_credentials(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-env-key")

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "providers": {
                "shared": {
                  "type": "openai",
                  "api_key": "config-openai-key",
                  "models": ["gpt-5.4-mini"]
                }
              }
            }
            """,
        )

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings)

        assert resolved.provider_name == "shared"
        assert resolved.provider_type == "openai"
        assert resolved.model == "gpt-5.4-mini"
        assert resolved.api_key == "config-openai-key"

    def test_resolve_provider_prefers_deepseek_before_openrouter_in_auto_discovery(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-env-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-env-key")

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings)

        assert resolved.provider_name == "deepseek"
        assert resolved.provider_type == "deepseek"
        assert resolved.model == "deepseek-chat"
        assert resolved.api_key == "deepseek-env-key"

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

    def test_resolve_provider_uses_configured_api_key_env_var_before_default_env(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("OPENAI_API_KEY", "default-env-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-env-key")

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "providers": {
                "router": {
                  "type": "openai_chat",
                  "api_key": "${OPENROUTER_API_KEY}",
                  "base_url": "https://openrouter.ai/api/v1",
                  "models": ["openai/gpt-5"]
                }
              },
              "default": {
                "provider": "router",
                "model": "openai/gpt-5"
              }
            }
            """,
        )

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings)

        assert resolved.api_key == "router-env-key"

    def test_resolve_provider_errors_when_configured_api_key_env_var_is_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("OPENAI_API_KEY", "default-env-key")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "providers": {
                "router": {
                  "type": "openai_chat",
                  "api_key": "${OPENROUTER_API_KEY}",
                  "base_url": "https://openrouter.ai/api/v1",
                  "models": ["openai/gpt-5"]
                }
              },
              "default": {
                "provider": "router",
                "model": "openai/gpt-5"
              }
            }
            """,
        )

        settings = get_settings(str(workspace))

        with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
            resolve_provider(settings)

    def test_resolve_provider_ignores_reasoning_effort_for_openai_chat(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-env-key")

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "providers": {
                "router": {
                  "type": "openai_chat",
                  "api_key": "${OPENROUTER_API_KEY}",
                  "base_url": "https://openrouter.ai/api/v1",
                  "models": ["openai/gpt-5"],
                  "reasoning_effort": "high"
                }
              },
              "default": {
                "provider": "router"
              }
            }
            """,
        )

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings)

        assert resolved.provider_type == "openai_chat"
        assert resolved.reasoning_effort is None

    def test_resolve_provider_does_not_fallback_away_from_default_provider(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("OPENAI_API_KEY", "openai-env-key")

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "providers": {
                "claude": {
                  "type": "anthropic",
                  "models": ["claude-sonnet-4-6"]
                }
              },
              "default": {
                "provider": "claude"
              }
            }
            """,
        )

        settings = get_settings(str(workspace))

        with pytest.raises(ValueError, match="provider 'claude' is selected"):
            resolve_provider(settings)

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

    def test_provider_without_models_uses_builtin_defaults(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "providers": {
                "moonshotai": {
                  "type": "moonshotai"
                }
              },
              "default": {
                "provider": "moonshotai"
              }
            }
            """,
        )

        settings = get_settings(str(workspace))

        assert settings.providers["moonshotai"].models == ["kimi-k2.5"]

    def test_resolve_provider_errors_when_no_providers_are_available(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))

        settings = get_settings(str(workspace))

        with pytest.raises(ValueError, match="no available providers found"):
            resolve_provider(settings)

    def test_resolve_provider_uses_builtin_default_model_for_raw_provider(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings, provider_name="openai")

        assert resolved.provider_type == "openai"
        assert resolved.model == "gpt-5.4"
        assert resolved.api_key == "env-key"
        assert resolved.max_tokens == 8192

    def test_resolve_provider_applies_catalog_metadata(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        monkeypatch.setattr(
            "mycode.core.config.lookup_model_metadata",
            lambda **_: ModelMetadata(
                provider="openai",
                model="gpt-4.1-mini",
                name="GPT-4.1 mini",
                context_window=1_000_000,
                max_input_tokens=500_000,
                max_output_tokens=32_768,
                supports_reasoning=False,
                supports_tools=True,
                raw={},
            ),
        )

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "providers": {
                "shared": {
                  "type": "openai",
                  "reasoning_effort": "high",
                  "models": ["gpt-4.1-mini"]
                }
              },
              "default": {
                "provider": "shared"
              }
            }
            """,
        )

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings)

        assert resolved.model == "gpt-4.1-mini"
        assert resolved.max_tokens == 32_768
        assert resolved.context_window == 1_000_000
        assert resolved.reasoning_effort is None

    def test_resolve_provider_uses_global_default_reasoning_effort(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        monkeypatch.setattr(
            "mycode.core.config.lookup_model_metadata",
            lambda **_: ModelMetadata(
                provider="openai",
                model="gpt-5.4",
                name="GPT-5.4",
                context_window=400_000,
                max_input_tokens=272_000,
                max_output_tokens=128_000,
                supports_reasoning=True,
                supports_tools=True,
                raw={},
            ),
        )

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "default": {
                "provider": "openai",
                "reasoning_effort": "high"
              }
            }
            """,
        )

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings)

        assert resolved.provider_type == "openai"
        assert resolved.reasoning_effort == "high"

    def test_resolve_provider_rejects_unsupported_reasoning_effort(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "default": {
                "provider": "openai",
                "reasoning_effort": "minimal"
              }
            }
            """,
        )

        settings = get_settings(str(workspace))

        with pytest.raises(ValueError, match="unsupported reasoning_effort 'minimal'"):
            resolve_provider(settings)

    def test_resolve_provider_keeps_default_behavior_when_provider_has_no_effort_support(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-env-key")

        _write(
            home / ".mycode" / "config.json",
            """
            {
              "providers": {
                "router": {
                  "type": "openai_chat",
                  "api_key": "${OPENROUTER_API_KEY}",
                  "base_url": "https://openrouter.ai/api/v1",
                  "models": ["openai/gpt-5"]
                }
              },
              "default": {
                "provider": "router",
                "reasoning_effort": "high"
              }
            }
            """,
        )

        settings = get_settings(str(workspace))
        resolved = resolve_provider(settings)

        assert resolved.provider_type == "openai_chat"
        assert resolved.reasoning_effort is None


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


def test_lookup_model_metadata_matches_exact_provider_type(monkeypatch) -> None:
    fake_catalog = {"zai": {"models": {"glm-5.1": {"id": "glm-5.1", "limit": {"output": 131_072}}}}}
    monkeypatch.setattr("mycode.core.models.load_models_dev", lambda **_: fake_catalog)

    metadata = lookup_model_metadata(
        provider_type="zai",
        provider_name="zhipu-coding",
        model="glm-5.1",
        api_base="https://open.bigmodel.cn/api/coding/paas/v4",
    )

    assert metadata is not None
    assert metadata.provider == "zai"
    assert metadata.model == "glm-5.1"
    assert metadata.max_output_tokens == 131_072


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
    assert metadata.model == "glm-5.1"
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
