"""Workspace context shared by the CLI's tools and permission hooks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from mycode.events import CancelledEvent, ErrorEvent, Event
from mycode.messages import ConversationMessage, flatten_message_text
from mycode.usage import Cost, Usage
from mycode_cli.background import BackgroundJobs
from mycode_cli.config import WebConfig
from mycode_cli.sessions import SessionStore, sum_session_totals

if TYPE_CHECKING:
    from mycode.agent import Agent
    from mycode_cli.agents import AgentDefinition

ChildStatus = Literal["completed", "failed", "cancelled"]


@dataclass(frozen=True)
class ChildOutcome:
    """How one subagent turn ended, computed from the records it added to the child session."""

    status: ChildStatus
    # The text of the turn's last assistant message.
    text: str
    # Why the turn did not complete: ``cancelled``, ``max_turns reached``, a provider error.
    error: str | None
    tool_calls: int
    duration_ms: int
    usage: Usage
    cost: Cost | None

    @classmethod
    def from_records(
        cls,
        records: list[ConversationMessage],
        *,
        terminal: ErrorEvent | CancelledEvent | None,
        duration_ms: int,
    ) -> ChildOutcome:
        """``terminal`` is the turn's terminal event; ``None`` for a turn that completed."""

        assistant = [record for record in records if record["role"] == "assistant"]
        totals = sum_session_totals(records)
        match terminal:
            case CancelledEvent():
                status, error = "cancelled", "cancelled"
            case ErrorEvent(message=message):
                status, error = "failed", message
            case None:
                status, error = "completed", None
        return cls(
            status=status,
            text=flatten_message_text(assistant[-1]) if assistant else "",
            error=error,
            tool_calls=sum(1 for record in assistant for block in record["content"] if block["type"] == "tool_use"),
            duration_ms=duration_ms,
            usage=totals.usage,
            cost=totals.cost,
        )

    def stats(self) -> dict[str, Any]:
        """The fields every UI-facing record of the turn carries: tool result metadata, job meta, ``agent.json``."""

        return {
            "status": self.status,
            "tool_calls": self.tool_calls,
            "duration_ms": self.duration_ms,
            "usage": self.usage,
            "cost": self.cost,
        }


@dataclass(eq=False)
class ChildTurn:
    """One subagent turn, run by the host in a task it owns.

    The tool never cancels ``task``: cancelling ``achat()`` from outside skips
    the agent's own cleanup. It calls ``cancel()``, which latches so a turn
    whose task has not reached ``achat()`` yet ends cancelled without a request.
    ``steer()`` likewise holds a prompt that arrives before the run takes
    steers until the host reports the run's first event.
    """

    agent: Agent
    # Every event of the turn; ends once ``task`` is done.
    events: AsyncIterator[Event]
    # Owns the turn; set by the host right after construction.
    task: asyncio.Task[None] = field(init=False)
    # Set by the host before ``task`` finishes.
    outcome: ChildOutcome | None = None
    cancel_requested: bool = False
    # Prompts steered before the run started; ``None`` once the run has them.
    early_steers: list[str] | None = field(default_factory=list, init=False)

    def cancel(self) -> None:
        self.cancel_requested = True
        self.agent.cancel()

    def steer(self, prompt: str) -> bool:
        """Hand ``prompt`` to the running turn; ``False`` when it is stopping or finishing."""

        if self.early_steers is None:
            return self.agent.steer(prompt)
        # A turn that ended before its run started never takes the prompt.
        if self.cancel_requested or self.outcome is not None:
            return False
        self.early_steers.append(prompt)
        return True

    def run_started(self) -> None:
        """Called by the host at each event of the turn's run, in the run's task.

        From the first event on, the run takes steers until its final step,
        so the prompts held until then go to the agent. A run already stopped
        or failed at its first event refuses them, and they are lost with that
        turn, like any steer a stop discards.
        """

        if self.early_steers is None:
            return
        prompts, self.early_steers = self.early_steers, None
        for prompt in prompts:
            self.agent.steer(prompt)


# Called by the host with the turn's outcome before its task finishes.
OnChildComplete = Callable[[ChildOutcome], Awaitable[None]]


class AgentHost(Protocol):
    """Builds and runs the subagents of one parent agent for the ``agent`` tool."""

    definitions: list[AgentDefinition]
    # The models a call may choose, the first being the default: model reference -> ``(provider name, model)``.
    models: dict[str, tuple[str, str]]
    # Serializes the turns of each child session in this process.
    locks: dict[str, asyncio.Lock]

    @property
    def children(self) -> SessionStore:
        """The store of the parent's child sessions (``<parent>/agents``)."""
        ...

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
        """Build the child agent for ``session_id``, loading its history; return it and its provider name.

        ``endpoint`` is the ``(provider name, model)`` to run on: the one a
        resumed child keeps, or the model the call chose; otherwise the
        definition's model, the first of ``models``, or the parent's is used.
        ``effort``, else the parent's, is rounded up to one that model accepts.
        Raises ``ValueError`` when the model does not resolve.
        """
        ...

    def start_turn(
        self, agent: Agent, message: str, *, session_id: str, description: str, on_complete: OnChildComplete
    ) -> ChildTurn:
        """Start one turn of ``agent`` in a task the host owns."""
        ...


@dataclass(frozen=True)
class CliDeps:
    """Application context injected into tools/hooks via ``Agent(deps=...)``."""

    cwd: Path
    # Scratch area for large tool outputs (bash spill files, webfetch dumps):
    # ``SessionStore.tool_output_dir`` for the session.
    tool_output_dir: Path
    # Providers for the webfetch and websearch tools.
    web: WebConfig
    # The session's background job registry; ``None`` when the host cannot deliver results.
    jobs: BackgroundJobs | None = None
    # Runs the ``agent`` tool's subagents; ``None`` where the tool is unavailable (inside a subagent).
    agents: AgentHost | None = None


def resolve_path(path: str | Path, *, cwd: str | Path) -> Path:
    """Resolve ``path`` against ``cwd``; absolute and ``~`` paths pass through."""

    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    return p.resolve(strict=False)
