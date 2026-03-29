"""CLI session selection and runtime updates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mycode.core.agent import Agent
from mycode.core.config import ResolvedProvider, Settings, get_settings, provider_has_api_key, resolve_provider
from mycode.core.models import lookup_model_metadata
from mycode.core.providers import (
    get_provider_adapter,
    list_env_discoverable_providers,
    provider_api_key_from_env,
    provider_default_models,
)
from mycode.core.session import SessionStore

REASONING_EFFORT_OPTIONS = ("auto", "none", "low", "medium", "high", "xhigh")


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


def build_agent(
    *,
    store: SessionStore,
    cwd: str,
    settings: Settings,
    resolved_provider: ResolvedProvider,
    resolved_session: ResolvedSession,
    max_turns: int | None,
) -> Agent:
    """Build the CLI agent from the resolved provider and session."""

    return Agent(
        model=resolved_provider.model,
        provider=resolved_provider.provider,
        cwd=cwd,
        session_dir=store.session_dir(resolved_session.session_id),
        session_id=resolved_session.session_id,
        api_key=resolved_provider.api_key,
        api_base=resolved_provider.api_base,
        messages=resolved_session.messages,
        settings=settings,
        reasoning_effort=resolved_provider.reasoning_effort,
        max_tokens=resolved_provider.max_tokens,
        context_window=resolved_provider.context_window,
        compact_threshold=settings.compact_threshold,
        max_turns=max_turns,
    )


def clone_agent(agent: Agent, *, store: SessionStore, session_id: str, messages: list[dict[str, Any]]) -> Agent:
    """Keep the current runtime config while swapping session state."""

    return Agent(
        model=agent.model,
        provider=agent.provider,
        cwd=agent.cwd,
        session_dir=store.session_dir(session_id),
        session_id=session_id,
        api_key=agent.api_key,
        api_base=agent.api_base,
        messages=messages,
        max_turns=agent.max_turns,
        max_tokens=agent.max_tokens,
        reasoning_effort=agent.reasoning_effort,
        settings=agent.settings,
    )


async def append_session_message(
    store: SessionStore,
    session_id: str,
    message: dict[str, Any],
    *,
    agent: Agent,
) -> None:
    """Persist one message with the current agent runtime metadata."""

    await store.append_message(
        session_id,
        message,
        provider=agent.provider,
        model=agent.model,
        cwd=agent.cwd,
        api_base=agent.api_base,
    )


def list_provider_options(settings: Settings) -> list[ProviderOption]:
    """Return configured providers plus env-discovered built-ins."""

    options: list[ProviderOption] = []
    configured_types: set[str] = set()

    for name, config in settings.providers.items():
        raw_models = config.models or list(provider_default_models(config.type))
        models = tuple(dict.fromkeys(model.strip() for model in raw_models if model.strip()))
        options.append(
            ProviderOption(
                name=name,
                provider=config.type,
                models=models,
                api_base=config.base_url,
            )
        )
        if provider_has_api_key(config):
            configured_types.add(config.type)

    for provider_name in list_env_discoverable_providers():
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


def get_provider_option(settings: Settings, *, provider: str, api_base: str | None) -> ProviderOption | None:
    """Return the current selectable provider option."""

    for option in list_provider_options(settings):
        if option.provider == provider and option.api_base == api_base:
            return option
    return None


def list_model_options(settings: Settings, *, provider: str, api_base: str | None, current_model: str) -> list[str]:
    """Return the selectable model list for the current provider runtime."""

    models = [current_model, *provider_default_models(provider)]
    for option in list_provider_options(settings):
        if option.provider == provider and option.api_base == api_base:
            models = [current_model, *option.models]
            break
    return list(dict.fromkeys(models))


def supports_reasoning_effort(agent: Agent) -> bool:
    """Return whether the current agent provider+model supports reasoning effort."""

    adapter = get_provider_adapter(agent.provider)
    if not adapter.supports_reasoning_effort:
        return False
    meta = lookup_model_metadata(
        provider_type=agent.provider,
        provider_name=agent.provider,
        model=agent.model,
    )
    return meta is not None and meta.supports_reasoning is True


def update_reasoning_effort(agent: Agent, effort: str | None) -> bool:
    """Update reasoning effort on the in-memory agent."""

    if effort == agent.reasoning_effort:
        return False
    agent.reasoning_effort = effort
    return True


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
        return ResolvedSession(
            requested_session_id,
            data.get("session") or {},
            data.get("messages") or [],
            "resumed",
        )

    if continue_last:
        latest = await store.latest_session(cwd=cwd)
        if latest and latest.get("id"):
            session_id = str(latest["id"])
            data = await store.load_session(session_id)
            if not data:
                raise ValueError(f"Unknown session: {session_id}")
            return ResolvedSession(
                session_id,
                data.get("session") or latest,
                data.get("messages") or [],
                "resumed",
            )

    data = store.draft_session(None, provider=provider, model=model, cwd=cwd, api_base=api_base)
    session = data.get("session") or {}
    return ResolvedSession(str(session.get("id") or ""), session, [], "new")


async def update_agent_runtime(
    agent: Agent,
    *,
    provider_name: str | None,
    model: str | None,
) -> bool:
    """Update provider-related request settings on the active agent.

    This refreshes settings from disk and changes the in-memory agent runtime
    only. Stored session metadata remains unchanged.
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

    agent.provider = resolved.provider
    agent.model = resolved.model
    agent.api_key = resolved.api_key
    agent.api_base = resolved.api_base
    agent.reasoning_effort = resolved.reasoning_effort
    agent.max_tokens = resolved.max_tokens
    agent.settings = settings
    return runtime_changed
