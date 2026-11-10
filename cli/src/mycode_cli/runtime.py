"""Agent construction shared by the TUI, ``mycode run``, and the web server."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from mycode.agent import Agent
from mycode.events import CancelledEvent, ErrorEvent, Event
from mycode.tools import ToolSpec
from mycode_cli.agent_tool import agent_tool
from mycode_cli.agents import AgentDefinition, discover_agents
from mycode_cli.background import BackgroundJobs
from mycode_cli.config import ResolvedProvider, Settings, find_model, resolve_provider
from mycode_cli.permissions import (
    ToolReviewCallback,
    ToolReviewDecision,
    ToolReviewRequest,
    build_permission_hooks,
    narrow_permission,
    usable_tools,
)
from mycode_cli.sessions import SessionStore
from mycode_cli.system_prompt import Skill, build_system_prompt, discover_skills
from mycode_cli.tools import DEFAULT_TOOLS
from mycode_cli.web_tools import build_web_tools
from mycode_cli.workspace import AgentHost, ChildOutcome, ChildTurn, CliDeps, OnChildComplete

logger = logging.getLogger(__name__)

_SUBAGENT_PROMPT = """\
You were started by another agent to carry out the task in the first message. \
Your final message is returned to that agent and is not shown to the user, \
so make it self-contained and concise: the result, and the facts the agent needs to use it.\
"""


def build_agent(
    *,
    store: SessionStore,
    settings: Settings,
    resolved_provider: ResolvedProvider,
    session_id: str,
    max_turns: int | None = None,
    review: ToolReviewCallback | None = None,
    jobs: BackgroundJobs | None = None,
    agents: AgentHost | None = None,
) -> Agent:
    """Build an agent from the resolved provider, honoring model config overrides.

    History is auto-loaded from disk when ``session_id`` already exists under
    the store; callers never pass messages explicitly. With an ``agents``
    host the agent gets the ``agent`` tool.
    """

    skills = discover_skills(settings.cwd)
    tools = [*DEFAULT_TOOLS, *build_web_tools(settings.web)]
    if agents is not None:
        tools.append(agent_tool(agents.definitions, list(agents.models)))
    return _new_agent(
        store=store,
        settings=settings,
        resolved_provider=resolved_provider,
        session_id=session_id,
        skills=skills,
        system=build_system_prompt(settings.cwd, skills),
        tools=tools,
        review=review,
        max_turns=max_turns,
        deps=CliDeps(
            cwd=Path(settings.cwd),
            tool_output_dir=store.tool_output_dir(session_id),
            web=settings.web,
            jobs=jobs,
            agents=agents,
        ),
    )


def build_child_agent(
    *,
    store: SessionStore,
    settings: Settings,
    resolved_provider: ResolvedProvider,
    parent_session_id: str,
    review: ToolReviewCallback | None,
    definition: AgentDefinition,
    session_id: str,
    description: str,
    background: bool,
    endpoint: tuple[str, str] | None = None,
    default_endpoint: tuple[str, str] | None = None,
    effort: str | None = None,
) -> tuple[Agent, str]:
    """Build the subagent ``session_id`` of the parent session; return it and its provider name.

    ``resolved_provider`` and ``review`` are the parent's. The child runs on
    ``endpoint``, a ``(provider name, model)``, else the model
    ``definition.model`` names (``ValueError`` when it does not resolve), else
    ``default_endpoint``, else the parent's model. Its effort is ``effort``,
    else the parent's, rounded up to one its model accepts
    (``ResolvedProvider.with_effort_at_least``). It has no ``agent`` tool and
    no job registry. Its permission is the parent's narrowed by the
    definition's, in deny mode when it runs in the background, since nobody
    can answer; it gets only the tools that permission can allow.
    """

    resolved = resolved_provider
    if endpoint is None and definition.model is not None:
        found = find_model(settings, definition.model, prefer=resolved_provider.provider_name)
        if found is None:
            raise ValueError(f"model {definition.model!r} of agent {definition.name!r} is not available")
        endpoint = found
    endpoint = endpoint or default_endpoint
    if endpoint is not None:
        resolved = resolve_provider(settings, provider_name=endpoint[0], model=endpoint[1])
    resolved = resolved.with_effort_at_least(effort or resolved_provider.reasoning_effort)

    permission = narrow_permission(settings.permission, definition.permission)
    if background:
        permission = replace(permission, mode="deny")
    tools = usable_tools([*DEFAULT_TOOLS, *build_web_tools(settings.web)], permission)

    child_review: ToolReviewCallback | None = None
    if review is not None:
        parent_review = review

        async def child_review(request: ToolReviewRequest) -> ToolReviewDecision:
            return await parent_review(replace(request, agent=description))

    children = SessionStore(data_dir=store.agents_dir(parent_session_id))
    skills = discover_skills(settings.cwd)
    system = "\n\n".join(
        part for part in (build_system_prompt(settings.cwd, skills), _SUBAGENT_PROMPT, definition.body) if part
    )
    agent = _new_agent(
        store=children,
        settings=replace(settings, permission=permission),
        resolved_provider=resolved,
        session_id=session_id,
        skills=skills,
        system=system,
        tools=tools,
        review=child_review,
        max_turns=definition.max_turns,
        deps=CliDeps(cwd=Path(settings.cwd), tool_output_dir=children.tool_output_dir(session_id), web=settings.web),
    )
    return agent, resolved.provider_name


def _new_agent(
    *,
    store: SessionStore,
    settings: Settings,
    resolved_provider: ResolvedProvider,
    session_id: str,
    skills: list[Skill],
    system: str,
    tools: Sequence[ToolSpec],
    review: ToolReviewCallback | None,
    max_turns: int | None,
    deps: CliDeps,
) -> Agent:
    metadata = resolved_provider.metadata
    return Agent(
        model=resolved_provider.model,
        provider=resolved_provider.provider,
        session_dir=store.data_dir,
        session_id=session_id,
        api_key=resolved_provider.api_key,
        api_base=resolved_provider.api_base,
        reasoning_effort=resolved_provider.reasoning_effort,
        legacy_max_tokens=resolved_provider.legacy_max_tokens,
        max_tokens=metadata.max_output_tokens,
        context_window=metadata.context_window,
        supports_image_input=metadata.supports_image_input,
        supports_pdf_input=metadata.supports_pdf_input,
        compact_threshold=settings.compact_threshold,
        max_turns=max_turns,
        system=system,
        tools=tools,
        hooks=build_permission_hooks(settings, skills=skills, review=review),
        deps=deps,
    )


@dataclass
class LocalAgentHost:
    """The ``AgentHost`` of the TUI and ``mycode run``: each child turn runs in a
    task of this process, its events passed on through a queue.

    Holds the parent's context; build a new host whenever the parent agent is rebuilt.
    """

    store: SessionStore
    settings: Settings
    resolved_provider: ResolvedProvider
    session_id: str
    review: ToolReviewCallback | None = None
    definitions: list[AgentDefinition] = field(init=False)
    models: dict[str, tuple[str, str]] = field(init=False)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.definitions = discover_agents(self.settings.cwd)
        self.models = {}
        for reference in self.settings.subagent.models:
            # Only models that resolve are offered.
            try:
                endpoint = find_model(self.settings, reference, prefer=self.resolved_provider.provider_name)
                if endpoint is None:
                    raise ValueError("no available provider lists it")
                resolve_provider(self.settings, provider_name=endpoint[0], model=endpoint[1])
            except ValueError as exc:
                logger.warning("Subagent model %r is not available: %s", reference, exc)
                continue
            self.models[reference] = endpoint

    @property
    def children(self) -> SessionStore:
        return SessionStore(data_dir=self.store.agents_dir(self.session_id))

    def build(
        self,
        definition: AgentDefinition,
        *,
        session_id: str,
        description: str,
        background: bool,
        endpoint: tuple[str, str] | None = None,
        effort: str | None = None,
    ) -> tuple[Agent, str]:
        return build_child_agent(
            store=self.store,
            settings=self.settings,
            resolved_provider=self.resolved_provider,
            parent_session_id=self.session_id,
            review=self.review,
            definition=definition,
            session_id=session_id,
            description=description,
            background=background,
            endpoint=endpoint,
            # The first usable model of ``subagent.models``.
            default_endpoint=next(iter(self.models.values()), None),
            effort=effort,
        )

    def start_turn(
        self, agent: Agent, message: str, *, session_id: str, description: str, on_complete: OnChildComplete
    ) -> ChildTurn:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()

        async def events() -> AsyncIterator[Event]:
            while (event := await queue.get()) is not None:
                yield event

        turn = ChildTurn(agent=agent, events=events())

        async def drive() -> None:
            started = time.monotonic()
            first = len(agent.messages)
            terminal: ErrorEvent | CancelledEvent | None = CancelledEvent()
            if not turn.cancel_requested:
                terminal = None
                try:
                    async for event in agent.achat(message):
                        turn.run_started()
                        queue.put_nowait(event)
                        if isinstance(event, ErrorEvent | CancelledEvent):
                            terminal = event
                except Exception as exc:
                    # Session persistence failures raise instead of ending with an error event.
                    terminal = ErrorEvent(message=str(exc) or type(exc).__name__)
            turn.outcome = ChildOutcome.from_records(
                agent.messages[first:],
                terminal=terminal,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            await on_complete(turn.outcome)

        turn.task = asyncio.create_task(drive())
        # The events end only once the turn, outcome included, is done.
        turn.task.add_done_callback(lambda _: queue.put_nowait(None))
        return turn
