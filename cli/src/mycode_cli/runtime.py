"""CLI session selection and runtime updates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from mycode.agent import Agent
from mycode.providers import (
    get_provider_adapter,
    list_env_discoverable_providers,
    provider_api_key_from_env,
    provider_default_models,
)
from mycode.session import SessionStore
from mycode.tools import DEFAULT_TOOL_SPECS
from mycode_cli.config import ModelConfig, ResolvedProvider, Settings, provider_has_api_key
from mycode_cli.permissions import build_permission_hooks
from mycode_cli.system_prompt import build_system_prompt


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
    session_id: str,
    max_turns: int | None = None,
    reasoning_effort: str | None = None,
) -> Agent:
    """Build an agent from the resolved provider, honoring model config overrides.

    History is auto-loaded from disk when ``session_id`` already exists under
    the store; callers never pass messages explicitly.
    """

    model_config = model_config_for(settings, resolved_provider)
    agent = Agent(
        model=resolved_provider.model,
        provider=resolved_provider.provider,
        cwd=cwd,
        session_dir=store.data_dir,
        session_id=session_id,
        api_key=resolved_provider.api_key,
        api_base=resolved_provider.api_base,
        reasoning_effort=reasoning_effort if reasoning_effort is not None else resolved_provider.reasoning_effort,
        max_tokens=model_config.max_output_tokens if model_config else None,
        context_window=model_config.context_window if model_config else None,
        supports_reasoning=model_config.supports_reasoning if model_config else None,
        supports_image_input=model_config.supports_image_input if model_config else None,
        supports_pdf_input=model_config.supports_pdf_input if model_config else None,
        compact_threshold=settings.compact_threshold,
        max_turns=max_turns,
        system=build_system_prompt(cwd, settings),
        tools=DEFAULT_TOOL_SPECS,
    )
    agent.hooks = build_permission_hooks(settings, on_user_denied=agent.cancel)
    return agent


def model_config_for(settings: Settings, resolved: ResolvedProvider) -> ModelConfig | None:
    """Return the user-configured overrides for the resolved provider+model, if any."""

    provider_config = settings.providers.get(resolved.provider_name or "")
    if provider_config is None:
        return None
    return provider_config.models.get(resolved.model)


def clone_agent(agent: Agent, *, store: SessionStore, session_id: str) -> Agent:
    """Keep the current runtime config while swapping session state.

    History auto-loads from disk when ``session_id`` exists under the store.
    """

    return Agent(
        model=agent.model,
        provider=agent.provider,
        cwd=agent.cwd,
        session_dir=store.data_dir,
        session_id=session_id,
        api_key=agent.api_key,
        api_base=agent.api_base,
        max_turns=agent.max_turns,
        max_tokens=agent.max_tokens,
        context_window=agent.context_window,
        compact_threshold=agent.compact_threshold,
        reasoning_effort=agent.reasoning_effort,
        supports_reasoning=agent.supports_reasoning,
        supports_image_input=agent.supports_image_input,
        supports_pdf_input=agent.supports_pdf_input,
        system=agent.system,
        tools=DEFAULT_TOOL_SPECS,
        hooks=agent.hooks,
    )


def list_provider_options(settings: Settings) -> list[ProviderOption]:
    """Return configured providers plus env-discovered built-ins."""

    options: list[ProviderOption] = []
    configured_types: set[str] = set()

    for name, config in settings.providers.items():
        models = tuple(config.models)
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

    option = get_provider_option(settings, provider=provider, api_base=api_base)
    models = option.models if option else provider_default_models(provider)
    return list(dict.fromkeys([current_model, *models]))


def supports_reasoning_effort(agent: Agent) -> bool:
    """Return whether the current agent provider+model supports reasoning effort."""

    return agent.supports_reasoning is True and get_provider_adapter(agent.provider).supports_reasoning_effort


async def resolve_session(
    *,
    store: SessionStore,
    cwd: str,
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

    # New sessions: the id is allocated here; the on-disk session is created
    # lazily by Agent.achat on the first persist.
    return ResolvedSession(uuid4().hex, {}, [], "new")


def apply_resolved_provider(agent: Agent, resolved: ResolvedProvider, settings: Settings) -> bool:
    """Copy runtime settings from a resolved provider onto an active agent.

    Returns whether any field actually changed. Does not touch session state.
    Re-derives model capability fields from metadata when the provider or model
    changes so the agent reports accurate support flags.
    """

    runtime_changed = (
        agent.provider != resolved.provider
        or agent.model != resolved.model
        or agent.api_base != resolved.api_base
        or agent.api_key != resolved.api_key
        or agent.reasoning_effort != resolved.reasoning_effort
    )

    agent.provider = resolved.provider
    agent.model = resolved.model
    agent.api_key = resolved.api_key
    agent.api_base = resolved.api_base
    agent.reasoning_effort = resolved.reasoning_effort

    if runtime_changed:
        model_config = model_config_for(settings, resolved)
        agent.refresh_capabilities(
            max_tokens=model_config.max_output_tokens if model_config else None,
            context_window=model_config.context_window if model_config else None,
            supports_reasoning=model_config.supports_reasoning if model_config else None,
            supports_image_input=model_config.supports_image_input if model_config else None,
            supports_pdf_input=model_config.supports_pdf_input if model_config else None,
        )
    return runtime_changed
