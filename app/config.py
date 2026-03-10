"""Application configuration and logging setup."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# config.json sits at project root (parent of app/)
_CONFIG_PATH = Path(__file__).parent.parent / "config.json"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    type: str  # maps directly to any_llm provider: openai | anthropic | gemini | …
    models: list[str]  # available model names (no provider prefix)
    api_key: str | None = None
    base_url: str | None = None


@dataclass(frozen=True)
class Settings:
    providers: dict[str, ProviderConfig]
    default_provider: str | None  # name key into providers
    default_model: str | None  # bare model name (no provider prefix)
    port: int
    skills_paths: list[str] = field(default_factory=list)  # extra skill directories

    @property
    def active_provider(self) -> ProviderConfig | None:
        if not self.default_provider:
            return None
        return self.providers.get(self.default_provider)


def _load_json_config() -> tuple[dict[str, ProviderConfig], str | None, str | None, list[str]]:
    """Parse config.json. Returns (providers, default_provider, default_model, skills_paths)."""
    if not _CONFIG_PATH.exists():
        return {}, None, None, []
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}, None, None, []

    providers: dict[str, ProviderConfig] = {}
    for name, p in (data.get("providers") or {}).items():
        models = p.get("models") or []
        # Accept both list and single string for convenience
        if isinstance(models, str):
            models = [models]
        providers[name] = ProviderConfig(
            name=name,
            type=p.get("type", "openai"),
            models=models,
            api_key=p.get("api_key") or None,
            base_url=p.get("base_url") or None,
        )

    default = data.get("default") or {}
    default_provider = default.get("provider") if isinstance(default, dict) else None
    default_model = default.get("model") if isinstance(default, dict) else None

    skills_cfg = data.get("skills") or {}
    skills_paths = skills_cfg.get("paths", []) if isinstance(skills_cfg, dict) else []
    if isinstance(skills_paths, str):
        skills_paths = [skills_paths]

    return providers, default_provider, default_model, skills_paths


def get_settings() -> Settings:
    """Load settings from config.json, with env var fallback when no JSON config exists."""
    providers, default_provider, default_model, skills_paths = _load_json_config()

    # Env var fallback: build a synthetic provider from env vars
    if not providers:
        env_model_raw = os.environ.get("MODEL", "")
        env_base = os.environ.get("BASE_URL")
        env_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")

        # Support legacy "provider:model" or "provider/model" format in MODEL env var
        if ":" in env_model_raw:
            ptype, env_model = env_model_raw.split(":", 1)
        elif "/" in env_model_raw:
            ptype, env_model = env_model_raw.split("/", 1)
        else:
            ptype, env_model = "openai", env_model_raw

        if env_model:
            providers["env"] = ProviderConfig(
                name="env",
                type=ptype,
                models=[env_model],
                api_key=env_key,
                base_url=env_base,
            )
            default_provider = "env"
            default_model = env_model

    return Settings(
        providers=providers,
        default_provider=default_provider,
        default_model=default_model,
        port=int(os.environ.get("PORT", "8000")),
        skills_paths=skills_paths,
    )


def setup_logging() -> None:
    """Configure default logging."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
