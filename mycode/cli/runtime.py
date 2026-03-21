"""Session and runtime helpers for the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mycode.core.agent import Agent
from mycode.core.config import Settings, get_settings, provider_has_api_key, resolve_provider
from mycode.core.providers import (
    list_auto_discoverable_providers,
    provider_api_key_from_env,
    provider_default_models,
)
from mycode.core.session import SessionStore


@dataclass
class ResolvedSession:
    """The session selected for the current CLI run.

    `mode` is either `"new"` or `"resumed"`.
    """

    session_id: str
    session: dict[str, Any]
    messages: list[dict[str, Any]]
    mode: str


@dataclass(frozen=True)
class ProviderOption:
    """A provider option shown in the interactive provider switcher."""

    name: str
    provider: str
    models: tuple[str, ...]
    api_base: str | None


def list_provider_options(settings: Settings) -> list[ProviderOption]:
    """Return configured providers plus env-discovered built-ins."""

    options: list[ProviderOption] = []
    configured_types: set[str] = set()

    for name, config in settings.providers.items():
        raw_models = config.models or list(provider_default_models(config.type))
        models = tuple(dict.fromkeys(model.strip() for model in raw_models if model.strip()))
        options.append(ProviderOption(name=name, provider=config.type, models=models, api_base=config.base_url))
        if provider_has_api_key(config):
            configured_types.add(config.type)

    for provider_name in list_auto_discoverable_providers():
        if provider_name in configured_types or not provider_api_key_from_env(provider_name):
            continue
        options.append(
            ProviderOption(
                name=provider_name,
                provider=provider_name,
                models=provider_default_models(provider_name),
                api_base=None,
            )
        )

    return options


def list_model_options(settings: Settings, *, provider: str, api_base: str | None, current_model: str) -> list[str]:
    """Return the model choices for the current provider runtime."""

    for option in list_provider_options(settings):
        if option.provider == provider and option.api_base == api_base:
            return list(dict.fromkeys([current_model, *option.models]))
    return list(dict.fromkeys([current_model, *provider_default_models(provider)]))


async def resolve_session(
    *,
    store: SessionStore,
    provider: str,
    cwd: str,
    model: str,
    api_base: str | None,
    requested_session_id: str | None,
    continue_last: bool,
) -> ResolvedSession:
    """Resolve which session the CLI should load before starting."""

    if requested_session_id:
        data = await store.load_session(requested_session_id)
        if not data or not data.get("session"):
            raise ValueError(f"Unknown session: {requested_session_id}")

        synced = await store.get_or_create(
            requested_session_id,
            provider=provider,
            model=model,
            cwd=cwd,
            api_base=api_base,
        )
        session = synced.get("session") or data["session"]
        messages = synced.get("messages") or data.get("messages") or []
        return ResolvedSession(requested_session_id, session, messages, "resumed")

    if continue_last:
        latest = await store.latest_session(cwd=cwd)
        if latest and latest.get("id"):
            session_id = str(latest["id"])
            data = await store.get_or_create(
                session_id,
                provider=provider,
                model=model,
                cwd=cwd,
                api_base=api_base,
            )
            return ResolvedSession(
                session_id,
                data.get("session") or latest,
                data.get("messages") or [],
                "resumed",
            )

    data = await store.create_session(None, provider=provider, model=model, cwd=cwd, api_base=api_base)
    session = data.get("session") or {}
    return ResolvedSession(str(session.get("id") or ""), session, [], "new")


async def update_agent_runtime(
    agent: Agent,
    *,
    store: SessionStore,
    session_id: str,
    provider_name: str | None,
    model: str | None,
) -> bool:
    """Update provider-related request settings on the active agent.

    This changes the in-memory agent runtime only. Existing session metadata is
    intentionally left unchanged so the saved session still reflects how it was
    originally created.
    """

    settings = get_settings(agent.cwd)
    resolved = resolve_provider(settings, provider_name=provider_name, model=model)

    runtime_changed = (
        agent.provider != resolved.provider
        or agent.model != resolved.model
        or agent.api_base != resolved.api_base
        or agent.api_key != resolved.api_key
        or agent.reasoning_effort != resolved.reasoning_effort
        or agent.max_tokens != resolved.max_tokens
    )

    # Keep the session present on disk, but do not rewrite its original meta.
    await store.get_or_create(
        session_id,
        provider=resolved.provider,
        model=resolved.model,
        cwd=agent.cwd,
        api_base=resolved.api_base,
    )

    agent.provider = resolved.provider
    agent.model = resolved.model
    agent.api_key = resolved.api_key
    agent.api_base = resolved.api_base
    agent.reasoning_effort = resolved.reasoning_effort
    agent.max_tokens = resolved.max_tokens
    agent.settings = settings
    return runtime_changed
