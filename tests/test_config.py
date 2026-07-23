"""Tests for config loading and provider resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycode.models import ModelMetadata
from mycode.providers import list_env_discoverable_providers, provider_env_api_key_names
from mycode.session import SessionStore
from mycode_cli.config import get_settings, resolve_provider
from mycode_cli.runtime import build_agent


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def patch_metadata(monkeypatch: pytest.MonkeyPatch, metadata: ModelMetadata | None) -> None:
    monkeypatch.setattr("mycode.models.lookup_model_metadata", lambda **_: metadata)


def build_test_agent(tmp_path: Path, cwd: Path, settings, resolved):
    return build_agent(
        store=SessionStore(data_dir=tmp_path / "sessions"),
        cwd=str(cwd),
        settings=settings,
        resolved_provider=resolved,
        session_id="test",
    )


@pytest.fixture(autouse=True)
def clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for provider in list_env_discoverable_providers():
        for env_name in provider_env_api_key_names(provider):
            monkeypatch.delenv(env_name, raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def disable_live_models_dev_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_metadata(monkeypatch, None)


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home" / ".mycode"
    monkeypatch.setenv("MYCODE_HOME", str(home))
    return home


@pytest.fixture
def workspace(tmp_path: Path, config_home: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


class TestGetSettings:
    def test_merges_global_and_project_configs_from_project_to_cwd(self, tmp_path: Path, config_home: Path) -> None:
        project = tmp_path / "project"
        cwd = tmp_path / "project" / "apps" / "api"
        cwd.mkdir(parents=True)
        (project / ".git").mkdir()

        global_config = config_home / "config.json"
        project_config = project / ".mycode" / "config.json"
        cwd_config = cwd / ".mycode" / "config.json"
        write_json(
            global_config,
            {
                "providers": {
                    "shared": {
                        "type": "openai",
                        "api_key": "global-key",
                        "models": {"gpt-5-mini": {}},
                    }
                },
                "default": {"provider": "shared", "model": "gpt-5-mini"},
            },
        )
        write_json(
            project_config,
            {
                "providers": {
                    "shared": {
                        "base_url": "https://root.example/v1",
                    }
                },
            },
        )
        write_json(
            cwd_config,
            {
                "default": {"provider": "shared", "model": "gpt-5.5"},
                "providers": {
                    "shared": {
                        "models": {"gpt-5.5": {}},
                    }
                },
            },
        )

        settings = get_settings(str(cwd.resolve()))

        assert settings.cwd == str(cwd.resolve())
        assert settings.project == str(project.resolve())
        assert settings.default_provider == "shared"
        assert settings.default_model == "gpt-5.5"
        assert settings.providers["shared"].api_key == "global-key"
        assert settings.providers["shared"].base_url == "https://root.example/v1"
        assert list(settings.providers["shared"].models) == ["gpt-5.5"]
        assert settings.config_paths == [
            str(global_config.resolve()),
            str(project_config.resolve()),
            str(cwd_config.resolve()),
        ]

    def test_uses_cwd_as_project_when_no_git_is_found(self, tmp_path: Path, config_home: Path) -> None:
        project = tmp_path / "project"
        cwd = project / "apps" / "api"
        cwd.mkdir(parents=True)
        write_json(project / ".mycode" / "config.json", {"default": {"provider": "parent"}})
        write_json(cwd / ".mycode" / "config.json", {"default": {"provider": "local"}})

        settings = get_settings(str(cwd.resolve()))

        assert settings.project == str(cwd.resolve())
        assert settings.default_provider == "local"
        assert settings.config_paths == [str((cwd / ".mycode" / "config.json").resolve())]

    def test_ignores_legacy_env_without_config(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, config_home: Path
    ) -> None:
        monkeypatch.setenv("MODEL", "openai:gpt-5.4")
        monkeypatch.setenv("BASE_URL", "https://env.example/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")

        settings = get_settings(str(workspace.resolve()))

        assert settings.providers == {}
        assert settings.default_provider is None
        assert settings.default_model is None

    def test_ignores_agents_compat_config(self, tmp_path: Path, workspace: Path, config_home: Path) -> None:
        write_json(
            tmp_path / "home" / ".agents" / "config.json",
            {
                "default": {"provider": "compat"},
                "providers": {
                    "compat": {
                        "type": "openai",
                        "models": {"gpt-5.5": {}},
                    }
                },
            },
        )

        settings = get_settings(str(workspace.resolve()))

        assert settings.providers == {}
        assert settings.default_provider is None
        assert settings.config_paths == []

    def test_builtin_provider_without_models_uses_builtin_defaults(self, workspace: Path, config_home: Path) -> None:
        write_json(
            config_home / "config.json",
            {
                "providers": {"moonshotai": {"type": "moonshotai"}},
                "default": {"provider": "moonshotai"},
            },
        )

        settings = get_settings(str(workspace.resolve()))

        assert list(settings.providers["moonshotai"].models) == ["kimi-k3", "kimi-k2.6"]

    def test_builtin_provider_alias_can_omit_type(
        self, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-env-key")
        write_json(
            config_home / "config.json",
            {
                "providers": {
                    "openrouter": {
                        "models": {"deepseek/deepseek-v3.2": {}},
                    }
                },
                "default": {"provider": "openrouter"},
            },
        )

        settings = get_settings(str(workspace.resolve()))
        resolved = resolve_provider(settings)

        assert settings.providers["openrouter"].type == "openrouter"
        assert list(settings.providers["openrouter"].models) == ["deepseek/deepseek-v3.2"]
        assert resolved.provider == "openrouter"
        assert resolved.model == "deepseek/deepseek-v3.2"
        assert resolved.api_key == "router-env-key"

    def test_custom_provider_alias_requires_type(self, workspace: Path, config_home: Path) -> None:
        write_json(
            config_home / "config.json",
            {
                "providers": {
                    "custom-provider": {
                        "base_url": "https://custom-endpoint.example/v1",
                    }
                }
            },
        )

        with pytest.raises(ValueError, match="provider 'custom-provider' must set 'type'"):
            get_settings(str(workspace.resolve()))

    def test_rejects_unsupported_reasoning_effort_value(self, workspace: Path, config_home: Path) -> None:
        write_json(
            config_home / "config.json",
            {
                "default": {
                    "provider": "openai",
                    "reasoning_effort": "minimal",
                }
            },
        )

        with pytest.raises(ValueError, match="unsupported reasoning_effort 'minimal'"):
            get_settings(str(workspace.resolve()))

    def test_loads_permission_config(self, workspace: Path, config_home: Path) -> None:
        write_json(
            config_home / "config.json",
            {"permission": {"level": "readonly", "mode": "deny"}},
        )
        write_json(
            workspace / ".mycode" / "config.json",
            {"permission": "standard"},
        )

        settings = get_settings(str(workspace.resolve()))

        assert settings.permission.level == "standard"
        assert settings.permission.mode == "deny"

    def test_rejects_invalid_permission_value(self, workspace: Path, config_home: Path) -> None:
        write_json(config_home / "config.json", {"permission": {"level": "careless"}})

        with pytest.raises(ValueError, match="unsupported permission level 'careless'"):
            get_settings(str(workspace.resolve()))


class TestResolveProvider:
    @pytest.mark.parametrize(
        ("provider_name", "env_name", "env_value", "model", "expected_model"),
        [
            ("moonshotai", "MOONSHOT_API_KEY", "moonshot-env-key", "kimi-k2-thinking", "kimi-k2-thinking"),
            ("google", "GEMINI_API_KEY", "gemini-env-key", None, "gemini-3.6-flash"),
        ],
    )
    def test_accepts_raw_supported_providers(
        self,
        workspace: Path,
        config_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        provider_name: str,
        env_name: str,
        env_value: str,
        model: str | None,
        expected_model: str,
    ) -> None:
        monkeypatch.setenv(env_name, env_value)

        resolved = resolve_provider(get_settings(str(workspace.resolve())), provider_name=provider_name, model=model)

        assert resolved.provider == provider_name
        assert resolved.model == expected_model
        assert resolved.api_key == env_value

    def test_uses_configured_api_key_and_base_url_before_default_env(
        self, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        monkeypatch.setenv("BASE_URL", "https://env.example/v1")
        write_json(
            config_home / "config.json",
            {
                "providers": {
                    "shared": {
                        "type": "anthropic",
                        "api_key": "config-key",
                        "base_url": "https://config.example/v1",
                        "models": {"claude-sonnet-4-6": {}},
                    }
                },
                "default": {
                    "provider": "shared",
                    "model": "claude-sonnet-4-6",
                },
            },
        )

        resolved = resolve_provider(get_settings(str(workspace.resolve())))

        assert resolved.provider == "anthropic"
        assert resolved.model == "claude-sonnet-4-6"
        assert resolved.api_key == "config-key"
        assert resolved.api_base == "https://config.example/v1"

    def test_explicit_api_key_override_wins_over_configured_key(
        self, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        write_json(
            config_home / "config.json",
            {
                "providers": {
                    "claude": {
                        "type": "anthropic",
                        "api_key": "config-key",
                        "models": {"claude-sonnet-4-6": {}},
                    }
                },
                "default": {
                    "provider": "claude",
                    "model": "claude-sonnet-4-6",
                },
            },
        )

        resolved = resolve_provider(get_settings(str(workspace.resolve())), api_key="request-key")

        assert resolved.api_key == "request-key"

    def test_reads_api_key_from_configured_env_var_before_default_env(
        self, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "default-env-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-env-key")
        write_json(
            config_home / "config.json",
            {
                "providers": {
                    "router": {
                        "type": "openai_chat",
                        "api_key": "${OPENROUTER_API_KEY}",
                        "base_url": "https://openrouter.ai/api/v1",
                        "models": {"openai/gpt-5": {}},
                    }
                },
                "default": {"provider": "router", "model": "openai/gpt-5"},
            },
        )

        resolved = resolve_provider(get_settings(str(workspace.resolve())))

        assert resolved.api_key == "router-env-key"

    def test_falls_back_when_default_provider_api_key_is_missing(
        self, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "openai-env-key")
        write_json(
            config_home / "config.json",
            {
                "providers": {
                    "router": {
                        "type": "openai_chat",
                        "api_key": "${OPENROUTER_API_KEY}",
                        "base_url": "https://openrouter.ai/api/v1",
                        "models": {"openai/gpt-5": {}},
                    }
                },
                "default": {"provider": "router", "model": "openai/gpt-5"},
            },
        )

        resolved = resolve_provider(get_settings(str(workspace.resolve())))

        assert resolved.provider == "openai"
        assert resolved.api_key == "openai-env-key"

    def test_auto_discovers_first_env_provider(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "openai-env-key")
        monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-env-key")

        resolved = resolve_provider(get_settings(str(workspace.resolve())))

        assert resolved.provider_name == "openai"
        assert resolved.provider == "openai"
        assert resolved.model == "gpt-5.6-sol"
        assert resolved.api_key == "openai-env-key"

    def test_auto_discovery_prefers_configured_provider_with_credentials(
        self, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-env-key")
        write_json(
            config_home / "config.json",
            {
                "providers": {
                    "shared": {
                        "type": "openai",
                        "api_key": "config-openai-key",
                        "models": {"gpt-5.4-mini": {}},
                    }
                }
            },
        )

        resolved = resolve_provider(get_settings(str(workspace.resolve())))

        assert resolved.provider_name == "shared"
        assert resolved.provider == "openai"
        assert resolved.model == "gpt-5.4-mini"
        assert resolved.api_key == "config-openai-key"

    def test_auto_discovery_prefers_deepseek_before_openrouter(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-env-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-env-key")

        resolved = resolve_provider(get_settings(str(workspace.resolve())))

        assert resolved.provider_name == "deepseek"
        assert resolved.provider == "deepseek"
        assert resolved.model == "deepseek-v4-pro"
        assert resolved.api_key == "deepseek-env-key"

    def test_explicit_provider_name_does_not_fall_back(
        self, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "openai-env-key")
        write_json(
            config_home / "config.json",
            {
                "providers": {
                    "claude": {
                        "type": "anthropic",
                        "models": {"claude-sonnet-4-6": {}},
                    }
                },
                "default": {"provider": "claude"},
            },
        )

        with pytest.raises(ValueError, match="provider 'claude' is selected"):
            resolve_provider(
                get_settings(str(workspace.resolve())),
                provider_name="claude",
            )

    def test_ignores_anthropic_auth_token_env(self, workspace: Path, config_home: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "env-token")

        with pytest.raises(ValueError, match="no available providers found") as exc_info:
            resolve_provider(get_settings(str(workspace.resolve())))

        assert "ANTHROPIC_API_KEY" in str(exc_info.value)
        assert "ANTHROPIC_AUTH_TOKEN" not in str(exc_info.value)

    def test_errors_when_no_providers_are_available(self, workspace: Path, config_home: Path) -> None:
        with pytest.raises(ValueError, match="no available providers found"):
            resolve_provider(get_settings(str(workspace.resolve())))

    def test_raw_provider_uses_builtin_agent_defaults_without_catalog_metadata(
        self, tmp_path: Path, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")

        settings = get_settings(str(workspace.resolve()))
        resolved = resolve_provider(settings, provider_name="openai")
        agent = build_test_agent(tmp_path, workspace, settings, resolved)

        assert resolved.provider == "openai"
        assert resolved.model == "gpt-5.6-sol"
        assert agent.max_tokens == 16_384
        assert agent.context_window == 128_000


class TestAgentCapabilities:
    def test_catalog_metadata_drives_agent_capabilities(
        self, tmp_path: Path, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        patch_metadata(
            monkeypatch,
            ModelMetadata(
                provider="openai",
                model="gpt-4.1-mini",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_reasoning=False,
                supports_image_input=True,
                supports_pdf_input=True,
            ),
        )
        write_json(
            config_home / "config.json",
            {
                "providers": {
                    "shared": {
                        "type": "openai",
                        "reasoning_effort": "high",
                        "models": {"gpt-4.1-mini": {}},
                    }
                },
                "default": {"provider": "shared"},
            },
        )

        settings = get_settings(str(workspace.resolve()))
        resolved = resolve_provider(settings)
        agent = build_test_agent(tmp_path, workspace, settings, resolved)

        assert resolved.reasoning_effort is None
        assert agent.max_tokens == 32_768
        assert agent.context_window == 1_000_000
        assert agent.supports_image_input is True
        assert agent.supports_pdf_input is True

    def test_applies_global_reasoning_effort_only_when_supported(
        self, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        patch_metadata(
            monkeypatch,
            ModelMetadata(
                provider="openai",
                model="gpt-5.5",
                context_window=400_000,
                max_output_tokens=128_000,
                supports_reasoning=True,
                supports_image_input=None,
                supports_pdf_input=None,
            ),
        )
        write_json(
            config_home / "config.json",
            {
                "default": {
                    "provider": "openai",
                    "reasoning_effort": "high",
                }
            },
        )

        resolved = resolve_provider(get_settings(str(workspace.resolve())))

        assert resolved.provider == "openai"
        assert resolved.reasoning_effort == "high"

    def test_drops_reasoning_effort_for_providers_without_support(
        self, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-env-key")
        write_json(
            config_home / "config.json",
            {
                "providers": {
                    "router": {
                        "type": "openai_chat",
                        "api_key": "${OPENROUTER_API_KEY}",
                        "base_url": "https://openrouter.ai/api/v1",
                        "models": {"openai/gpt-5": {}},
                    }
                },
                "default": {
                    "provider": "router",
                    "reasoning_effort": "high",
                },
            },
        )

        resolved = resolve_provider(get_settings(str(workspace.resolve())))

        assert resolved.provider == "openai_chat"
        assert resolved.reasoning_effort is None

    def test_config_model_metadata_overrides_catalog_values(
        self, tmp_path: Path, workspace: Path, config_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        patch_metadata(
            monkeypatch,
            ModelMetadata(
                provider="openai",
                model="gpt-5.5",
                context_window=400_000,
                max_output_tokens=128_000,
                supports_reasoning=True,
                supports_image_input=None,
                supports_pdf_input=None,
            ),
        )
        write_json(
            config_home / "config.json",
            {
                "providers": {
                    "openai": {
                        "models": {
                            "gpt-5.5": {
                                "context_window": 500_000,
                                "max_output_tokens": 64_000,
                                "supports_reasoning": False,
                                "supports_image_input": True,
                            }
                        }
                    }
                },
                "default": {"provider": "openai"},
            },
        )

        settings = get_settings(str(workspace.resolve()))
        resolved = resolve_provider(settings)
        agent = build_test_agent(tmp_path, workspace, settings, resolved)

        assert agent.context_window == 500_000
        assert agent.max_tokens == 64_000
        assert agent.supports_reasoning is False
        assert agent.supports_image_input is True
