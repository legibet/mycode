"""The ``agent`` tool: delegate a self-contained task to a subagent.

A subagent is a child session under ``<parent>/agents/<id>/``, with a generated
``id``. Every call names its child in its metadata as soon as the child is set
up, before the turn runs. A blocking call streams one summary line per child
step and returns the child's final reply; a background
call registers a ``BackgroundJob`` whose result reaches the parent as a job
notification. ``resume`` continues a finished child with a new turn, or steers
the prompt into a running background child. The host (``CliDeps.agents``)
builds the child and runs its turn in a task it owns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from mycode.agent import Agent
from mycode.events import Event, TextEvent, ToolStartEvent, UsageEvent
from mycode.messages import text_block
from mycode.tools import ToolContext, ToolExecutionResult, ToolOutputCallback, ToolSpec, tool
from mycode_cli.agents import AgentDefinition, format_agents
from mycode_cli.background import BackgroundJob, BackgroundJobs
from mycode_cli.sessions import ID_PATTERN, SessionLock, SessionLockedError
from mycode_cli.workspace import AgentHost, ChildOutcome, ChildTurn, CliDeps

logger = logging.getLogger(__name__)

# Agent ids are directory names and API path segments.
AGENT_ID_RE = re.compile(ID_PATTERN)
AGENT_INFO_FILE = "agent.json"
# The fields of agent.json, and of its ``last`` outcome, that readers index.
_INFO_FIELDS = ("agent", "description", "provider", "model", "created_at")
_LAST_FIELDS = ("status", "finished_at")
_LOCK_HOLDER = "a subagent turn"
_SUMMARY_MAX_CHARS = 200

# Summary line per tool: label and the input field it shows.
_TOOL_SUMMARIES = {
    "read": ("Read", "path"),
    "write": ("Write", "path"),
    "edit": ("Edit", "path"),
    "bash": ("Bash", "command"),
    "webfetch": ("WebFetch", "url"),
    "websearch": ("WebSearch", "query"),
}

_DESCRIPTION = (
    "Start a subagent: a separate agent that works on a self-contained task in its own, initially empty "
    "conversation. Use it for broad research or independent pieces of work whose details you do not need "
    "in your own context. Calls issued together run in parallel.\n\n"
    "A blocking call returns the agent's final reply. With background=true the call returns at once and "
    "the agent's result is delivered to you when it finishes, even after your turn ends: do not poll, and "
    "end your turn if nothing else is left to do. A blocking agent's permission requests are shown to the user "
    "as usual, labelled with the agent; a background agent cannot ask, so its tool calls above the configured "
    "permission level are denied."
)


class _AgentError(Exception):
    """A refused call; the message becomes the ``error:`` result."""


@tool(
    name="agent",
    description=_DESCRIPTION,
    parameters={
        "description": "Three to five words naming the task, shown to the user.",
        "prompt": (
            "The task. The agent starts with an empty conversation and does not see yours, "
            "so include every detail it needs."
        ),
        "agent": "Name of the agent to run. Defaults to general.",
        "model": "Model to run the agent on.",
        "effort": (
            "Reasoning effort for the task. Defaults to yours; "
            "on a model without this level, the next higher one it has is used."
        ),
        "background": "Run the agent in the background and return at once; its result is delivered when it finishes.",
        "resume": (
            "Agent id from an earlier call: continue that agent with prompt instead of starting a new one. "
            "A running background agent receives prompt as a message."
        ),
    },
    streams_output=True,
    parallel_safe=True,
)
async def _agent_tool(
    ctx: ToolContext[CliDeps],
    description: str,
    prompt: str,
    agent: str = "general",
    model: str | None = None,
    effort: Literal["low", "medium", "high", "xhigh"] | None = None,
    background: bool = False,
    resume: str | None = None,
) -> ToolExecutionResult:
    """Run a subagent turn, blocking or in the background."""

    host = ctx.deps.agents
    assert host is not None, "the agent tool is registered only with an agent host"
    jobs = ctx.deps.jobs
    tool_use_id = ctx.tool_call_id or ""
    if resume is not None and not AGENT_ID_RE.fullmatch(resume):
        return _error(f"invalid agent id: {resume!r}")
    if background and jobs is None:
        return _error("background agents are not available in this mode")

    session_id = resume or uuid4().hex
    # Parallel calls on one child run one after the other. A blocking turn
    # holds the lock to its end; a background turn only until its job is
    # registered, from when a resume steers the job instead.
    lock = host.locks.setdefault(session_id, asyncio.Lock())
    await lock.acquire()
    if resume is not None and jobs is not None and (job := jobs.find(resume)) is not None:
        lock.release()
        return await _steer(host, job, resume, prompt)

    label = " ".join(description.split())
    # Reading and writing the child's files, its history and system prompt
    # sources included, blocks: the setup runs in a thread.
    setup = asyncio.ensure_future(
        asyncio.to_thread(
            _setup_child,
            host,
            session_id=session_id,
            agent_name=agent,
            model=model,
            effort=effort,
            resume=resume is not None,
            description=label,
            background=background,
        )
    )
    try:
        child_lock, child, info = await asyncio.shield(setup)
    except asyncio.CancelledError:
        # The thread runs on; whatever it took is released once it is done.
        setup.add_done_callback(lambda done: _release_setup(done, lock))
        raise
    except (_AgentError, SessionLockedError, ValueError, OSError) as exc:
        lock.release()
        return _error(str(exc))
    except BaseException:
        lock.release()
        raise

    # Names the child while the call runs; every result carries it too.
    metadata = {"agent": info["agent"], "session_id": session_id, "background": background}
    if ctx.emit is not None:
        ctx.emit(metadata=metadata)
    info_path = host.children.session_dir(session_id) / AGENT_INFO_FILE

    async def on_complete(outcome: ChildOutcome) -> None:
        info["last"] = {**outcome.stats(), "finished_at": _now()}
        await asyncio.to_thread(_write_json, info_path, info)

    turn = host.start_turn(child, prompt, session_id=session_id, description=label, on_complete=on_complete)
    # Held for the whole turn, including the outcome written to agent.json.
    turn.task.add_done_callback(lambda _: child_lock.release())

    if background:
        assert jobs is not None
        # Started eagerly, so the drain is at its loop before the job is
        # registered: a job closed at once never cancels a drain that has not
        # run, which would leave nothing waiting for the child to stop.
        drain = asyncio.Task(
            _drain_background(jobs, turn, tool_use_id=tool_use_id, session_id=session_id, label=label),
            loop=asyncio.get_running_loop(),
            eager_start=True,
        )
        jobs.add(
            BackgroundJob(
                tool_use_id=tool_use_id,
                name="agent",
                label=label,
                started_at=_now(),
                task=drain,
                kill=turn.cancel,
                session_id=session_id,
                steer=turn.steer,
            )
        )
        lock.release()
        return ToolExecutionResult(
            output=f"Started agent in background: {label}\nAgent id: {session_id}", metadata=metadata
        )

    turn.task.add_done_callback(lambda _: lock.release())
    outcome = await _wait(turn, ctx.emit, ctx.deps.cwd)
    output = _reply(outcome, session_id)
    if outcome.status == "completed":
        seconds = round(outcome.duration_ms / 1000)
        calls = _tool_calls(outcome.tool_calls)
        output += f'\n\n[Agent {session_id} finished: {calls}, {seconds}s. Continue it with resume="{session_id}".]'
    return ToolExecutionResult(
        output=output,
        metadata={**metadata, **outcome.stats()},
        is_error=outcome.status != "completed",
    )


def agent_tool(definitions: list[AgentDefinition], models: list[str]) -> ToolSpec:
    """The ``agent`` tool, its description listing ``definitions``; ``model`` takes one of ``models``, if any."""

    properties = dict(_agent_tool.input_schema["properties"])
    model = properties.pop("model")
    if models:
        properties["model"] = {
            "type": "string",
            "enum": models,
            "description": f"{model['description']} Defaults to the agent's own model, else {models[0]}.",
        }
    return replace(
        _agent_tool,
        description=f"{_DESCRIPTION}\n\nAvailable agents:\n{format_agents(definitions)}",
        input_schema={**_agent_tool.input_schema, "properties": properties},
    )


def _setup_child(
    host: AgentHost,
    *,
    session_id: str,
    agent_name: str,
    model: str | None,
    effort: str | None,
    resume: bool,
    description: str,
    background: bool,
) -> tuple[SessionLock, Agent, dict[str, Any]]:
    """Take the child's lock, build the child, and write ``agent.json`` for a new one.

    The lock comes first: no other process writes the child while its
    metadata and history are read and written here. Returns the lock, the
    child, and its ``agent.json`` contents.
    """

    child_lock = host.children.lock(session_id, _LOCK_HOLDER)
    try:
        info_path = host.children.session_dir(session_id) / AGENT_INFO_FILE
        definitions = {definition.name: definition for definition in host.definitions}
        if resume:
            info = read_agent_info(info_path)
            if info is None:
                raise _AgentError(f"unknown agent id: {session_id}")
            definition = definitions.get(info["agent"])
            if definition is None:
                raise _AgentError(f"unknown agent: {info['agent']}")
            # A resumed child keeps its endpoint; the definition body is the current one.
            child, _ = host.build(
                definition,
                session_id=session_id,
                description=description,
                background=background,
                endpoint=(info["provider"], info["model"]),
            )
            # The previous outcome is gone until this turn ends: a crash leaves the child interrupted.
            if info.pop("last", None) is not None:
                _write_json(info_path, info)
        else:
            definition = definitions.get(agent_name)
            if definition is None:
                raise _AgentError(f"unknown agent: {agent_name}; available: {', '.join(definitions)}")
            endpoint = None
            if model is not None:
                endpoint = host.models.get(model)
                if endpoint is None:
                    raise _AgentError(f"unknown model: {model}; available: {', '.join(host.models) or 'none'}")
            child, provider_name = host.build(
                definition,
                session_id=session_id,
                description=description,
                background=background,
                endpoint=endpoint,
                effort=effort,
            )
            info = {
                "agent": definition.name,
                "description": description,
                "provider": provider_name,
                "model": child.model,
                "created_at": _now(),
            }
            _write_json(info_path, info)
    except BaseException:
        child_lock.release()
        raise
    return child_lock, child, info


def _release_setup(setup: asyncio.Future[tuple[SessionLock, Agent, dict[str, Any]]], lock: asyncio.Lock) -> None:
    """Release a setup whose call was cancelled: the child lock it took, then the call's lock."""

    if not setup.cancelled() and setup.exception() is None:
        setup.result()[0].release()
    lock.release()


async def _steer(host: AgentHost, job: BackgroundJob, session_id: str, prompt: str) -> ToolExecutionResult:
    """Hand ``prompt`` to the running background child ``session_id``."""

    assert job.steer is not None
    if not job.steer(prompt):
        return _error(f"agent {session_id} is finishing; wait for its result, then resume it")
    info = await asyncio.to_thread(read_agent_info, host.children.session_dir(session_id) / AGENT_INFO_FILE)
    return ToolExecutionResult(
        output=f"Message delivered to running agent {session_id}",
        metadata={"agent": info["agent"] if info else None, "session_id": session_id, "steered": True},
    )


async def _wait(turn: ChildTurn, emit: ToolOutputCallback | None, cwd: Path) -> ChildOutcome:
    """Follow a blocking turn to its end, emitting summary lines; a parent cancel stops the child."""

    summary = _Summary(emit, cwd)
    try:
        async for event in turn.events:
            summary.add(event)
        summary.flush()
        await asyncio.wait([turn.task])
    except asyncio.CancelledError:
        # The cancellation ends here, as in bash: the child stops through its
        # own cancel and persists its cancelled turn, which becomes the result.
        task = asyncio.current_task()
        assert task is not None
        task.uncancel()
        turn.cancel()
        await asyncio.wait([turn.task])
    return _outcome(turn)


async def _drain_background(
    jobs: BackgroundJobs, turn: ChildTurn, *, tool_use_id: str, session_id: str, label: str
) -> None:
    """Follow a background turn to its end, then hand its result to the registry.

    Cancelled by ``BackgroundJobs.close`` after ``kill`` stopped the child: it
    waits for the child to persist its cancelled turn and delivers nothing.
    """

    try:
        async for _ in turn.events:
            pass
        await asyncio.wait([turn.task])
    except asyncio.CancelledError:
        await asyncio.wait([turn.task])
        raise
    outcome = _outcome(turn)
    job = {"tool_use_id": tool_use_id, "name": "agent", "label": label, "session_id": session_id, **outcome.stats()}
    await jobs.notify(text_block(f"Agent finished: {label}\n\n{_reply(outcome, session_id)}", meta={"job": job}))


def _outcome(turn: ChildTurn) -> ChildOutcome:
    """The outcome of a finished turn; a failure after it was set (writing ``agent.json``) is only logged."""

    if not turn.task.cancelled() and (exc := turn.task.exception()) is not None:
        logger.error("subagent turn failed to finish", exc_info=exc)
    if turn.outcome is None:
        raise RuntimeError("subagent turn ended without an outcome")
    return turn.outcome


def _reply(outcome: ChildOutcome, session_id: str) -> str:
    """The child's final text, followed by the error of a turn that did not complete.

    A cancelled turn's error says how far the child got and how to continue it.
    """

    if outcome.error is None:
        return outcome.text or "(no output)"
    error = outcome.error
    if outcome.status == "cancelled":
        error = (
            f"cancelled by the user after {_tool_calls(outcome.tool_calls)}; "
            f'the agent\'s transcript is kept, continue it with resume="{session_id}"'
        )
    return f"{outcome.text}\n\nerror: {error}" if outcome.text else f"error: {error}"


def _tool_calls(count: int) -> str:
    return f"{count} tool call{'' if count == 1 else 's'}"


class _Summary:
    """One line per child step for the parent's live output: each tool call and
    the first line of each text reply. Display only; never persisted."""

    def __init__(self, emit: ToolOutputCallback | None, cwd: Path) -> None:
        self._emit = emit
        self._cwd = cwd
        self._text = ""
        self._sent = False

    def add(self, event: Event) -> None:
        match event:
            case TextEvent(delta=delta) if not self._sent:
                self._text += delta
                first, newline, _ = self._text.lstrip().partition("\n")
                if newline:
                    self._line(first)
                    self._sent = True
            case ToolStartEvent(tool_call=call):
                self.flush()
                label, field = _TOOL_SUMMARIES.get(call["name"], (call["name"], None))
                value = call["input"].get(field) if field else None
                if field == "path" and isinstance(value, str):
                    value = self._relative(value)
                self._line(f"{label} {value or ''}" if field else label)
            case UsageEvent():
                self.flush()

    def flush(self) -> None:
        """End the current reply: send its first line if no line ended yet."""

        if not self._sent and self._text.strip():
            self._line(self._text.strip().partition("\n")[0])
        self._text = ""
        self._sent = False

    def _relative(self, path: str) -> str:
        """``path`` relative to the workspace when it is inside it, else unchanged."""

        candidate = Path(path)
        if candidate.is_absolute() and candidate.is_relative_to(self._cwd):
            return str(candidate.relative_to(self._cwd))
        return path

    def _line(self, text: str) -> None:
        line = " ".join(str(text).split())
        if len(line) > _SUMMARY_MAX_CHARS:
            line = line[: _SUMMARY_MAX_CHARS - 1] + "…"
        if line and self._emit is not None:
            self._emit(line + "\n")


def read_agent_info(path: Path) -> dict[str, Any] | None:
    """Read a child's ``agent.json``; ``None`` when it is missing, unreadable, or lacks a field its readers use."""

    with suppress(OSError, ValueError):
        info = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(info, dict) and all(isinstance(info.get(key), str) for key in _INFO_FIELDS):
            last = info.get("last")
            if "last" not in info or (
                isinstance(last, dict) and all(isinstance(last.get(key), str) for key in _LAST_FIELDS)
            ):
                return info
    return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    # Replaced whole, so a reader never sees a partial file.
    temp = path.with_name(f"{path.name}.tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(output=f"error: {message}", is_error=True)


def _now() -> str:
    return datetime.now(UTC).isoformat()
