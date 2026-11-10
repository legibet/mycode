"""The agent tool: real child agents driven through the tool by a scripted provider."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import string
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from mycode import Agent, ToolDoneEvent, ToolOutputEvent
from mycode.messages import ContentBlock, MessageMeta, build_message, flatten_message_text
from mycode.models import resolve_model_metadata
from mycode.providers.base import MessageDone, ProviderRequest, ProviderStreamEvent, TextDelta
from mycode.tools import ToolContext, ToolExecutionResult, ToolOutputCallback
from mycode.usage import Usage
from mycode_cli.agents import AgentDefinition
from mycode_cli.background import BackgroundJob, BackgroundJobs
from mycode_cli.config import (
    ModelConfig,
    PermissionConfig,
    ProviderConfig,
    ResolvedProvider,
    Settings,
    SubagentConfig,
)
from mycode_cli.permissions import PERMISSION_DENIED_OUTPUT, ToolReviewDecision, ToolReviewRequest
from mycode_cli.runtime import LocalAgentHost, build_agent, build_child_agent
from mycode_cli.sessions import SessionLockedError, SessionStore
from mycode_cli.workspace import ChildOutcome, CliDeps

# A list of events, or a function streaming them for the request.
Turn = list[ProviderStreamEvent] | Callable[[ProviderRequest], AsyncIterator[ProviderStreamEvent]]

_USAGE: Usage = {"input_tokens": 100, "output_tokens": 10}


class _Provider:
    """Scripted provider turns per session id; records every request."""

    def __init__(self) -> None:
        self.scripts: dict[str, list[Turn]] = {}
        self.requests: list[ProviderRequest] = []

    def script(self, session_id: str, *turns: Turn) -> None:
        self.scripts.setdefault(session_id, []).extend(turns)

    def requests_for(self, session_id: str) -> list[ProviderRequest]:
        return [request for request in self.requests if request.session_id == session_id]

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        # The request shares the agent's live history; keep it as sent.
        self.requests.append(replace(request, messages=list(request.messages)))
        assert request.session_id is not None
        turn = self.scripts[request.session_id].pop(0)
        if isinstance(turn, list):
            for event in turn:
                yield event
        else:
            async for event in turn(request):
                yield event


def _assistant(*blocks: ContentBlock) -> list[ProviderStreamEvent]:
    tool_use = any(block["type"] == "tool_use" for block in blocks)
    meta: MessageMeta = {
        "usage": _USAGE.copy(),
        "cost": {"total": 0.01},
        "stop_reason": "tool_use" if tool_use else "stop",
    }
    return [MessageDone(build_message("assistant", list(blocks), meta=meta))]


def _text(text: str) -> list[ProviderStreamEvent]:
    return [TextDelta(text), *_assistant({"type": "text", "text": text})]


def _call(call_id: str, name: str, tool_input: dict[str, Any]) -> list[ProviderStreamEvent]:
    return _assistant({"type": "tool_use", "id": call_id, "name": name, "input": tool_input})


def _gated(gate: asyncio.Event, started: asyncio.Event, text: str) -> Turn:
    """A turn that streams ``partial`` and then waits for ``gate`` before finishing with ``text``."""

    async def turn(_request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        yield TextDelta("partial")
        started.set()
        await gate.wait()
        for event in _text(text):
            yield event

    return turn


@pytest.fixture
def provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Provider:
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home" / ".mycode"))
    # A resume resolves the child's stored provider again.
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr("mycode_cli.system_prompt.Path.home", lambda: tmp_path / "home")
    fake = _Provider()
    monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _provider: fake)
    return fake


@pytest.fixture(autouse=True)
def child_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """New children are named child_a, child_b, ... in start order, never after their call."""

    names = (f"child_{letter}" for letter in string.ascii_lowercase)
    monkeypatch.setattr("mycode_cli.agent_tool.uuid4", lambda: SimpleNamespace(hex=next(names)))


def _resolved(effort: str | None = None) -> ResolvedProvider:
    return ResolvedProvider(
        provider_name="openai",
        provider="openai",
        model="gpt-5.5",
        api_key="k",
        api_base=None,
        metadata=resolve_model_metadata(provider="openai", model="gpt-5.5"),
        reasoning_efforts=("low", "high"),
        reasoning_effort=effort,
    )


def _settings(tmp_path: Path, *, permission: PermissionConfig | None = None, **kwargs: Any) -> Settings:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return Settings(cwd=str(work), project=str(work), permission=permission or PermissionConfig(), **kwargs)


def _parent(
    tmp_path: Path,
    *,
    jobs: BackgroundJobs | None = None,
    permission: PermissionConfig | None = None,
    review: Callable[[ToolReviewRequest], Any] | None = None,
) -> Agent:
    store = SessionStore(data_dir=tmp_path / "sessions")
    settings = _settings(tmp_path, permission=permission, providers={})
    resolved = _resolved()
    host = LocalAgentHost(
        store=store, settings=settings, resolved_provider=resolved, session_id="parent", review=review
    )
    return build_agent(
        store=store,
        settings=settings,
        resolved_provider=resolved,
        session_id="parent",
        review=review,
        jobs=jobs,
        agents=host,
    )


def _deps(parent: Agent) -> CliDeps:
    return cast(CliDeps, parent.deps)


def _host(parent: Agent) -> LocalAgentHost:
    return cast(LocalAgentHost, _deps(parent).agents)


async def _call_tool(
    parent: Agent, call_id: str, emit: ToolOutputCallback | None = None, **args: Any
) -> ToolExecutionResult:
    args.setdefault("description", "Find things")
    args.setdefault("prompt", "find the things")
    ctx = ToolContext(executor=parent.tools, deps=parent.deps, tool_call_id=call_id, emit=emit)
    return await parent.tools.aexecute("agent", args, ctx)


def _child_dir(parent: Agent, child_id: str) -> Path:
    return _host(parent).children.session_dir(child_id)


def _children(parent: Agent) -> list[str]:
    """The ids of the parent's child sessions on disk."""

    return sorted(path.name for path in _host(parent).children.data_dir.iterdir())


def _info(parent: Agent, child_id: str) -> dict[str, Any]:
    return json.loads((_child_dir(parent, child_id) / "agent.json").read_text())


def _add_agent_job(jobs: BackgroundJobs, session_id: str, *, steer: Callable[[str], bool]) -> None:
    """Register a live agent job that runs until the registry closes."""

    async def run() -> None:
        await asyncio.Event().wait()

    jobs.add(
        BackgroundJob(
            tool_use_id=f"call_{session_id}",
            name="agent",
            label="x",
            started_at="",
            task=asyncio.create_task(run()),
            kill=lambda: None,
            session_id=session_id,
            steer=steer,
        )
    )


async def _finish_jobs(jobs: BackgroundJobs) -> None:
    async with asyncio.timeout(5):
        await asyncio.gather(*(job.task for job in jobs.jobs))


class TestBlocking:
    async def test_returns_the_reply_with_metadata_and_streams_summary_lines(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        parent = _parent(tmp_path)
        (tmp_path / "work" / "notes.txt").write_text("hello\n")
        args = {"description": "Read the notes", "prompt": "summarize notes.txt"}
        provider.script("parent", _call("call_a", "agent", args), _text("ok"))
        provider.script("child_a", _call("c1", "read", {"path": "notes.txt"}), _text("Found it.\nDetails follow."))

        events = [event async for event in parent.achat("go")]

        done = next(event for event in events if isinstance(event, ToolDoneEvent))
        assert done.is_error is False
        assert done.output == (
            'Found it.\nDetails follow.\n\n[Agent child_a finished: 1 tool call, 0s. Continue it with resume="child_a".]'
        )
        assert done.metadata is not None
        assert isinstance(done.metadata.pop("duration_ms"), int)
        assert done.metadata == {
            "agent": "general",
            "session_id": "child_a",
            "background": False,
            "status": "completed",
            "tool_calls": 1,
            "usage": {"input_tokens": 200, "output_tokens": 20},
            "cost": {"total": pytest.approx(0.02)},
        }
        # The call names its child before the child's first step.
        [announced, *summary] = [event for event in events if isinstance(event, ToolOutputEvent)]
        assert announced == ToolOutputEvent(
            tool_use_id="call_a", output="", metadata={"agent": "general", "session_id": "child_a", "background": False}
        )
        assert "".join(event.output for event in summary) == "Read notes.txt\nFound it.\n"

        [first, _] = provider.requests_for("child_a")
        assert flatten_message_text(first.messages[0]) == "summarize notes.txt"
        assert "You were started by another agent" in first.system
        assert "agent" not in {tool["name"] for tool in first.tools}
        parent_tools = {tool["name"]: tool for tool in provider.requests_for("parent")[0].tools}
        assert "- general: " in parent_tools["agent"]["description"]

        assert _children(parent) == ["child_a"]
        info = _info(parent, "child_a")
        assert {key: info[key] for key in ("agent", "description", "provider", "model")} == {
            "agent": "general",
            "description": "Read the notes",
            "provider": "openai",
            "model": "gpt-5.5",
        }
        assert info["last"]["status"] == "completed"
        assert info["last"]["tool_calls"] == 1
        assert {"created_at", "finished_at"} <= {*info, *info["last"]}

        store = SessionStore(data_dir=tmp_path / "sessions")
        totals = await store.load_totals("parent")
        assert totals.usage == {"input_tokens": 400, "output_tokens": 40}
        assert totals.cost == {"total": pytest.approx(0.04)}

    async def test_summary_lines_show_workspace_paths_relative(self, tmp_path: Path, provider: _Provider) -> None:
        parent = _parent(tmp_path)
        work = tmp_path / "work"
        provider.script(
            "child_a",
            _assistant(
                {"type": "tool_use", "id": "c1", "name": "read", "input": {"path": str(work / "src" / "a.py")}},
                {"type": "tool_use", "id": "c2", "name": "read", "input": {"path": str(tmp_path / "b.py")}},
                {"type": "tool_use", "id": "c3", "name": "bash", "input": {"command": f"ls {work}"}},
            ),
            _text("done"),
        )
        lines: list[str] = []

        def emit(output: str = "", /, *, metadata: dict[str, Any] | None = None) -> None:
            if output:
                lines.append(output)

        await _call_tool(parent, "call_a", emit=emit)

        assert lines[:3] == [f"Read {Path('src') / 'a.py'}\n", f"Read {tmp_path / 'b.py'}\n", f"Bash ls {work}\n"]

    async def test_parent_cancel_stops_the_child_and_returns_its_partial_text(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        parent = _parent(tmp_path)
        started = asyncio.Event()
        provider.script("parent", _call("call_a", "agent", {"description": "Slow", "prompt": "p"}))
        provider.script("child_a", _gated(asyncio.Event(), started, "never"))

        async def run() -> list[Any]:
            return [event async for event in parent.achat("go")]

        task = asyncio.create_task(run())
        async with asyncio.timeout(5):
            await started.wait()
        parent.cancel()
        async with asyncio.timeout(5):
            events = await task

        done = next(event for event in events if isinstance(event, ToolDoneEvent))
        assert done.is_error is True
        assert done.output == (
            "partial\n\nerror: cancelled by the user after 0 tool calls; "
            'the agent\'s transcript is kept, continue it with resume="child_a"'
        )
        assert done.metadata is not None
        assert done.metadata["status"] == "cancelled"
        assert events[-1].type == "cancelled"
        children = _host(parent).children
        assert children.load_messages_sync("child_a")[-1]["meta"]["stop_reason"] == "cancelled"
        assert _info(parent, "child_a")["last"]["status"] == "cancelled"
        children.lock("child_a", "test").release()

    async def test_failure_is_an_error_result(self, tmp_path: Path, provider: _Provider) -> None:
        parent = _parent(tmp_path)
        provider.script("child_a", [MessageDone(build_message("assistant", [], meta={"stop_reason": "error"}))])

        result = await _call_tool(parent, "call_a")

        assert result.is_error is True
        assert result.output == "error: provider returned an error response"
        assert result.metadata is not None
        assert result.metadata["status"] == "failed"

    async def test_review_requests_name_the_agent(self, tmp_path: Path, provider: _Provider) -> None:
        reviewed: list[ToolReviewRequest] = []

        async def review(request: ToolReviewRequest) -> ToolReviewDecision:
            reviewed.append(request)
            return "deny"

        parent = _parent(tmp_path, review=review)
        provider.script("child_a", _call("c1", "bash", {"command": "pnpm install"}), _text("denied"))

        await _call_tool(parent, "call_a", description="Install deps")

        [request] = reviewed
        assert (request.tool_name, request.preview, request.agent) == ("bash", "pnpm install", "Install deps")

    async def test_each_call_starts_its_own_child_whatever_its_call_id(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        jobs = BackgroundJobs()
        parent = _parent(tmp_path, jobs=jobs)
        provider.script("child_a", _text("one"))
        provider.script("child_b", _text("two"))

        # Provider call ids need not be path-safe or unique.
        blocking = await _call_tool(parent, "functions.agent:0", prompt="first")
        background = await _call_tool(parent, "functions.agent:0", prompt="second", background=True)
        await _finish_jobs(jobs)

        assert blocking.is_error is False
        assert blocking.output.startswith(
            'one\n\n[Agent child_a finished: 0 tool calls, 0s. Continue it with resume="child_a".]'
        )
        assert blocking.metadata is not None
        assert blocking.metadata["session_id"] == "child_a"
        assert background.output == "Started agent in background: Find things\nAgent id: child_b"
        assert background.metadata == {"agent": "general", "session_id": "child_b", "background": True}
        [block] = jobs.pending
        assert (block["meta"]["job"]["tool_use_id"], block["meta"]["job"]["session_id"]) == (
            "functions.agent:0",
            "child_b",
        )
        assert _children(parent) == ["child_a", "child_b"]
        children = _host(parent).children
        assert [flatten_message_text(message) for message in children.load_messages_sync("child_a")] == ["first", "one"]
        assert [flatten_message_text(message) for message in children.load_messages_sync("child_b")] == [
            "second",
            "two",
        ]


class TestBackground:
    async def test_start_returns_at_once_and_delivers_a_job_notification(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        jobs = BackgroundJobs()
        parent = _parent(tmp_path, jobs=jobs)
        provider.script("child_a", _call("c1", "read", {"path": "missing.txt"}), _text("All done."))

        result = await _call_tool(parent, "call_b", background=True)

        assert result.is_error is False
        assert result.output == "Started agent in background: Find things\nAgent id: child_a"
        assert result.metadata == {"agent": "general", "session_id": "child_a", "background": True}
        [job] = jobs.jobs
        assert (job.name, job.label, job.session_id, job.pid) == ("agent", "Find things", "child_a", None)
        assert job.info() == {
            "tool_use_id": "call_b",
            "name": "agent",
            "label": "Find things",
            "started_at": job.started_at,
            "session_id": "child_a",
        }

        await _finish_jobs(jobs)

        [block] = jobs.pending
        assert block["text"] == "Agent finished: Find things\n\nAll done."
        meta = block["meta"]["job"]
        assert isinstance(meta.pop("duration_ms"), int)
        assert meta == {
            "tool_use_id": "call_b",
            "name": "agent",
            "label": "Find things",
            "session_id": "child_a",
            "status": "completed",
            "tool_calls": 1,
            "usage": {"input_tokens": 200, "output_tokens": 20},
            "cost": {"total": pytest.approx(0.02)},
        }
        assert jobs.jobs == []
        assert _info(parent, "child_a")["last"]["status"] == "completed"

    async def test_refused_without_a_job_registry(self, tmp_path: Path, provider: _Provider) -> None:
        parent = _parent(tmp_path)

        result = await _call_tool(parent, "call_b", background=True)

        assert result == ToolExecutionResult(
            output="error: background agents are not available in this mode", is_error=True
        )
        assert _children(parent) == []

    async def test_runs_in_deny_mode(self, tmp_path: Path, provider: _Provider) -> None:
        reviewed: list[ToolReviewRequest] = []

        async def review(request: ToolReviewRequest) -> ToolReviewDecision:
            reviewed.append(request)
            return "allow"

        jobs = BackgroundJobs()
        parent = _parent(tmp_path, jobs=jobs, review=review)
        provider.script("child_a", _call("c1", "bash", {"command": "pnpm install"}), _text("could not"))

        await _call_tool(parent, "call_b", background=True)
        await _finish_jobs(jobs)

        assert reviewed == []
        result = _host(parent).children.load_messages_sync("child_a")[2]["content"][0]
        assert result["type"] == "tool_result"
        assert result["output"] == (
            f"{PERMISSION_DENIED_OUTPUT}: bash call needs level standard, this agent allows safe"
        )

    async def test_stopped_child_delivers_how_to_continue_it(self, tmp_path: Path, provider: _Provider) -> None:
        jobs = BackgroundJobs()
        parent = _parent(tmp_path, jobs=jobs)
        started = asyncio.Event()
        provider.script("child_a", _call("c1", "read", {"path": "missing.txt"}), _gated(asyncio.Event(), started, "x"))

        await _call_tool(parent, "call_b", background=True)
        async with asyncio.timeout(5):
            await started.wait()
        assert jobs.kill("call_b")
        await _finish_jobs(jobs)

        [block] = jobs.pending
        assert block["text"] == (
            "Agent finished: Find things\n\npartial\n\nerror: cancelled by the user after 1 tool call; "
            'the agent\'s transcript is kept, continue it with resume="child_a"'
        )
        assert block["meta"]["job"]["status"] == "cancelled"

    async def test_close_cancels_the_child_and_delivers_nothing(self, tmp_path: Path, provider: _Provider) -> None:
        jobs = BackgroundJobs()
        parent = _parent(tmp_path, jobs=jobs)
        started = asyncio.Event()
        provider.script("child_a", _gated(asyncio.Event(), started, "never"))

        await _call_tool(parent, "call_b", background=True)
        async with asyncio.timeout(5):
            await started.wait()
            await jobs.close()

        assert jobs.pending == []
        assert _info(parent, "child_a")["last"]["status"] == "cancelled"
        _host(parent).children.lock("child_a", "test").release()

    async def test_close_right_after_the_start_waits_for_the_child_to_stop(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        jobs = BackgroundJobs()
        parent = _parent(tmp_path, jobs=jobs)
        provider.script("child_a", _gated(asyncio.Event(), asyncio.Event(), "never"))

        await _call_tool(parent, "call_b", background=True)
        async with asyncio.timeout(5):
            await jobs.close()

        host = _host(parent)
        assert not host.locks["child_a"].locked()
        host.children.lock("child_a", "test").release()
        assert jobs.pending == []
        assert jobs.jobs == []


class TestResume:
    async def test_finished_child_continues_with_its_history(self, tmp_path: Path, provider: _Provider) -> None:
        parent = _parent(tmp_path)
        provider.script("child_a", _text("one"), _text("two"))
        await _call_tool(parent, "call_a", prompt="first prompt", description="First")
        created_at = _info(parent, "child_a")["created_at"]

        result = await _call_tool(parent, "call_r", resume="child_a", prompt="second prompt", description="Second")

        assert result.output.startswith(
            'two\n\n[Agent child_a finished: 0 tool calls, 0s. Continue it with resume="child_a".]'
        )
        assert result.metadata is not None
        assert result.metadata["session_id"] == "child_a"
        assert result.metadata["usage"] == _USAGE
        second = provider.requests_for("child_a")[1]
        assert [flatten_message_text(message) for message in second.messages] == [
            "first prompt",
            "one",
            "second prompt",
        ]
        info = _info(parent, "child_a")
        assert (info["description"], info["created_at"]) == ("First", created_at)
        assert _children(parent) == ["child_a"]

    async def test_a_resume_of_a_running_blocking_child_waits_for_its_turn(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        parent = _parent(tmp_path)
        gate, started = asyncio.Event(), asyncio.Event()
        provider.script("child_a", _gated(gate, started, "one"), _text("two"))
        first = asyncio.create_task(_call_tool(parent, "call_a", prompt="first prompt"))
        async with asyncio.timeout(5):
            await started.wait()

        resume = asyncio.create_task(_call_tool(parent, "call_r", resume="child_a", prompt="second prompt"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not resume.done()
        assert len(provider.requests_for("child_a")) == 1

        gate.set()
        async with asyncio.timeout(5):
            await asyncio.gather(first, resume)
        second = provider.requests_for("child_a")[1]
        assert [flatten_message_text(message) for message in second.messages] == [
            "first prompt",
            "one",
            "second prompt",
        ]

    async def test_resumed_child_keeps_its_stored_provider(
        self, tmp_path: Path, provider: _Provider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = ProviderConfig(name="other", type="openai_chat", models={"openai/gpt-5.5": ModelConfig()}, api_key="k")
        store = SessionStore(data_dir=tmp_path / "sessions")
        # "other" lists the reference "openai/gpt-5.5" as a model id, so a model lookup would pick it.
        settings = _settings(tmp_path, providers={"other": other})
        resolved = _resolved()
        host = LocalAgentHost(store=store, settings=settings, resolved_provider=resolved, session_id="parent")
        parent = build_agent(
            store=store, settings=settings, resolved_provider=resolved, session_id="parent", agents=host
        )
        adapter_lookups: list[str] = []

        def get_adapter(provider_id: str) -> _Provider:
            adapter_lookups.append(provider_id)
            return provider

        monkeypatch.setattr("mycode.agent.get_provider_adapter", get_adapter)
        provider.script("child_a", _text("one"), _text("two"))
        await _call_tool(parent, "call_a")
        assert _info(parent, "child_a")["provider"] == "openai"
        adapter_lookups.clear()

        await _call_tool(parent, "call_r", resume="child_a", prompt="again")

        assert adapter_lookups == ["openai"]
        assert _info(parent, "child_a")["provider"] == "openai"

    async def test_resume_clears_the_last_outcome_while_the_turn_runs(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        parent = _parent(tmp_path)
        gate, started = asyncio.Event(), asyncio.Event()
        provider.script("child_a", _text("one"), _gated(gate, started, "two"))
        await _call_tool(parent, "call_a")
        assert "last" in _info(parent, "child_a")

        task = asyncio.create_task(_call_tool(parent, "call_r", resume="child_a"))
        async with asyncio.timeout(5):
            await started.wait()
        assert "last" not in _info(parent, "child_a")
        gate.set()
        async with asyncio.timeout(5):
            await task

        assert _info(parent, "child_a")["last"]["status"] == "completed"

    async def test_running_background_child_receives_the_prompt(self, tmp_path: Path, provider: _Provider) -> None:
        jobs = BackgroundJobs()
        parent = _parent(tmp_path, jobs=jobs)
        gate, started = asyncio.Event(), asyncio.Event()
        provider.script("child_a", _gated(gate, started, "first"), _text("second"))
        await _call_tool(parent, "call_b", background=True)
        async with asyncio.timeout(5):
            await started.wait()

        result = await _call_tool(parent, "call_r", resume="child_a", prompt="also this", background=False)
        gate.set()
        await _finish_jobs(jobs)

        assert result == ToolExecutionResult(
            output="Message delivered to running agent child_a",
            metadata={"agent": "general", "session_id": "child_a", "steered": True},
        )
        steered = provider.requests_for("child_a")[1].messages[-1]
        assert steered["meta"].get("steer") is True
        assert flatten_message_text(steered) == "also this"
        [block] = jobs.pending
        assert block["text"] == "Agent finished: Find things\n\nsecond"

    async def test_a_resume_issued_with_a_background_resume_steers_the_same_turn(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        jobs = BackgroundJobs()
        parent = _parent(tmp_path, jobs=jobs)
        provider.script("child_a", _text("one"))
        await _call_tool(parent, "call_a")
        again = {"description": "Again", "prompt": "again", "resume": "child_a", "background": True}
        more = {"description": "More", "prompt": "also this", "resume": "child_a"}
        provider.script(
            "parent",
            _assistant(
                {"type": "tool_use", "id": "call_b", "name": "agent", "input": again},
                {"type": "tool_use", "id": "call_r", "name": "agent", "input": more},
            ),
            _text("waiting"),
        )
        provider.script("child_a", _text("first"), _text("second"))

        async with asyncio.timeout(5):
            events = [event async for event in parent.achat("go")]
        await _finish_jobs(jobs)

        [resumed] = [event for event in events if isinstance(event, ToolDoneEvent) and event.tool_use_id == "call_r"]
        assert (resumed.output, resumed.metadata) == (
            "Message delivered to running agent child_a",
            {"agent": "general", "session_id": "child_a", "steered": True},
        )
        # One turn took the prompt: its second request carries it, and one result follows.
        steered = provider.requests_for("child_a")[2].messages[-1]
        assert steered["meta"].get("steer") is True
        assert flatten_message_text(steered) == "also this"
        [block] = jobs.pending
        assert block["text"] == "Agent finished: Again\n\nsecond"

    async def test_steer_refused_by_a_finishing_child_is_an_error(self, tmp_path: Path, provider: _Provider) -> None:
        jobs = BackgroundJobs()
        parent = _parent(tmp_path, jobs=jobs)
        _add_agent_job(jobs, "child_a", steer=lambda _message: False)

        result = await _call_tool(parent, "call_r", resume="child_a")

        assert result == ToolExecutionResult(
            output="error: agent child_a is finishing; wait for its result, then resume it", is_error=True
        )
        await jobs.close()

    async def test_unknown_id_is_an_error(self, tmp_path: Path, provider: _Provider) -> None:
        parent = _parent(tmp_path)

        result = await _call_tool(parent, "call_r", resume="nope")

        assert result == ToolExecutionResult(output="error: unknown agent id: nope", is_error=True)
        assert not _child_dir(parent, "nope").exists()

    async def test_child_locked_by_another_process_is_an_error(self, tmp_path: Path, provider: _Provider) -> None:
        parent = _parent(tmp_path)
        provider.script("child_a", _text("one"))
        await _call_tool(parent, "call_a")

        with _host(parent).children.lock("child_a", "the web server"):
            result = await _call_tool(parent, "call_r", resume="child_a")

        assert result.is_error is True
        assert result.output == f"error: session child_a is in use by the web server (pid {os.getpid()})"


class TestValidation:
    @pytest.mark.parametrize(
        ("resume", "message"),
        [
            ("../parent", "error: invalid agent id: '../parent'"),
            ("abc\n", "error: invalid agent id: 'abc\\n'"),
        ],
    )
    async def test_resume_ids_are_validated(
        self, tmp_path: Path, provider: _Provider, resume: str, message: str
    ) -> None:
        parent = _parent(tmp_path)

        result = await _call_tool(parent, "call_a", resume=resume)

        assert result == ToolExecutionResult(output=message, is_error=True)

    async def test_unknown_agent_and_unavailable_model_are_errors(self, tmp_path: Path, provider: _Provider) -> None:
        parent = _parent(tmp_path)
        host = _host(parent)
        host.definitions = [*host.definitions, AgentDefinition(name="custom", description="d", model="no-such-model")]

        unknown = await _call_tool(parent, "call_a", agent="nope")
        unavailable = await _call_tool(parent, "call_b", agent="custom")

        assert unknown == ToolExecutionResult(
            output="error: unknown agent: nope; available: general, custom", is_error=True
        )
        assert unavailable == ToolExecutionResult(
            output="error: model 'no-such-model' of agent 'custom' is not available", is_error=True
        )
        assert _children(parent) == []


class TestModelSelection:
    @staticmethod
    def _parent(tmp_path: Path, models: tuple[str, ...]) -> Agent:
        store = SessionStore(data_dir=tmp_path / "sessions")
        other = ProviderConfig(name="other", type="openai", models={"plain": ModelConfig()}, api_key="k")
        settings = _settings(tmp_path, providers={"other": other}, subagent=SubagentConfig(models=models))
        resolved = _resolved()
        host = LocalAgentHost(store=store, settings=settings, resolved_provider=resolved, session_id="parent")
        return build_agent(store=store, settings=settings, resolved_provider=resolved, session_id="parent", agents=host)

    def test_only_models_that_resolve_are_offered(
        self, tmp_path: Path, provider: _Provider, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            parent = self._parent(tmp_path, ("other/plain", "missing/x", "unlisted"))

        spec = parent.tools.get("agent")
        assert spec is not None
        assert spec.input_schema["properties"]["model"]["enum"] == ["other/plain"]
        assert "'missing/x' is not available" in caplog.text
        assert "'unlisted' is not available" in caplog.text

    def test_without_models_the_tool_has_no_model_parameter(self, tmp_path: Path, provider: _Provider) -> None:
        spec = _parent(tmp_path).tools.get("agent")

        assert spec is not None
        assert "model" not in spec.input_schema["properties"]

    async def test_a_chosen_model_runs_the_child_and_a_resume_keeps_it(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        parent = self._parent(tmp_path, ("other/plain",))
        provider.script("child_a", _text("one"), _text("two"))

        await _call_tool(parent, "call_a", model="other/plain")
        await _call_tool(parent, "call_r", resume="child_a")

        info = _info(parent, "child_a")
        assert (info["provider"], info["model"]) == ("other", "plain")
        assert [request.model for request in provider.requests_for("child_a")] == ["plain", "plain"]

    async def test_the_first_usable_model_is_the_default_after_the_definitions_own(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        parent = self._parent(tmp_path, ("missing/x", "other/plain"))
        host = _host(parent)
        host.definitions = [*host.definitions, AgentDefinition(name="own", description="d", model="openai/gpt-5.5")]
        provider.script("child_a", _text("one"))
        provider.script("child_b", _text("two"))

        await _call_tool(parent, "call_a")
        await _call_tool(parent, "call_b", agent="own")

        assert (_info(parent, "child_a")["provider"], _info(parent, "child_a")["model"]) == ("other", "plain")
        assert (_info(parent, "child_b")["provider"], _info(parent, "child_b")["model"]) == ("openai", "gpt-5.5")
        spec = parent.tools.get("agent")
        assert spec is not None
        assert spec.input_schema["properties"]["model"]["description"].endswith("else other/plain.")

    async def test_a_model_outside_the_list_is_an_error(self, tmp_path: Path, provider: _Provider) -> None:
        parent = self._parent(tmp_path, ("other/plain",))

        result = await _call_tool(parent, "call_a", model="openai/gpt-5.5")

        assert result == ToolExecutionResult(
            output="error: unknown model: openai/gpt-5.5; available: other/plain", is_error=True
        )
        assert _children(parent) == []


class TestChildSetup:
    async def test_a_readonly_deny_definition_has_read_and_bash_only_and_denies_writes(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        parent = _parent(tmp_path, permission=PermissionConfig(level="yolo"))
        host = _host(parent)
        readonly = AgentDefinition(
            name="research", description="d", permission=PermissionConfig(level="readonly", mode="deny")
        )
        host.definitions = [*host.definitions, readonly]
        provider.script(
            "child_a",
            _assistant(
                {"type": "tool_use", "id": "c1", "name": "write", "input": {"path": "made.txt", "content": "x"}},
                {"type": "tool_use", "id": "c2", "name": "bash", "input": {"command": "touch made.txt"}},
                {"type": "tool_use", "id": "c3", "name": "bash", "input": {"command": "ls | head"}},
            ),
            _text("nothing changed"),
        )

        result = await _call_tool(parent, "call_e", agent="research")

        assert result.output.startswith("nothing changed")
        request = provider.requests_for("child_a")[0]
        assert [tool["name"] for tool in request.tools] == ["read", "bash"]
        results = _host(parent).children.load_messages_sync("child_a")[2]["content"]
        outputs = [block["output"] for block in results if block["type"] == "tool_result"]
        assert outputs[0] == "error: unknown tool: write"
        assert outputs[1] == f"{PERMISSION_DENIED_OUTPUT}: bash call needs level standard, this agent allows readonly"
        assert not outputs[2].startswith(PERMISSION_DENIED_OUTPUT)
        assert not (tmp_path / "work" / "made.txt").exists()

    def test_a_background_child_gets_only_the_tools_its_deny_mode_can_allow(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        host = _host(_parent(tmp_path, permission=PermissionConfig(level="safe", mode="ask")))
        general = host.definitions[0]

        blocking, _ = host.build(general, session_id="child_a", description="d", background=False)
        background, _ = host.build(general, session_id="child_b", description="d", background=True)

        assert [spec.name for spec in blocking.tools.specs] == ["read", "write", "edit", "bash", "webfetch"]
        assert [spec.name for spec in background.tools.specs] == ["read", "write", "edit", "bash"]

    async def test_child_lock_is_held_for_the_turn(self, tmp_path: Path, provider: _Provider) -> None:
        jobs = BackgroundJobs()
        parent = _parent(tmp_path, jobs=jobs)
        gate, started = asyncio.Event(), asyncio.Event()
        provider.script("child_a", _gated(gate, started, "done"))
        await _call_tool(parent, "call_b", background=True)
        async with asyncio.timeout(5):
            await started.wait()
        children = _host(parent).children

        with pytest.raises(SessionLockedError, match="in use by a subagent turn"):
            children.lock("child_a", "test")
        gate.set()
        await _finish_jobs(jobs)

        children.lock("child_a", "test").release()
        assert not _host(parent).locks["child_a"].locked()

    async def test_a_prompt_steered_before_the_run_starts_reaches_the_turn(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        parent = _parent(tmp_path)
        host = _host(parent)
        child, _ = host.build(host.definitions[0], session_id="child_a", description="d", background=True)
        provider.script("child_a", _text("first"), _text("second"))

        async def on_complete(_outcome: ChildOutcome) -> None:
            pass

        turn = host.start_turn(child, "go", session_id="child_a", description="d", on_complete=on_complete)
        assert turn.steer("also this")
        async with asyncio.timeout(5):
            [_ async for _ in turn.events]

        assert turn.outcome is not None
        assert (turn.outcome.status, turn.outcome.text) == ("completed", "second")
        steered = provider.requests_for("child_a")[1].messages[-1]
        assert steered["meta"].get("steer") is True
        assert flatten_message_text(steered) == "also this"

    async def test_a_call_stopped_during_setup_leaves_the_child_free(
        self, tmp_path: Path, provider: _Provider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent = _parent(tmp_path)
        host = _host(parent)
        building, proceed = threading.Event(), threading.Event()
        build = host.build

        def slow_build(*args: Any, **kwargs: Any) -> tuple[Agent, str]:
            building.set()
            proceed.wait(5)
            return build(*args, **kwargs)

        monkeypatch.setattr(host, "build", slow_build)
        call = asyncio.create_task(_call_tool(parent, "call_a"))
        await asyncio.to_thread(building.wait, 5)
        call.cancel()
        proceed.set()
        async with asyncio.timeout(5):
            with suppress(asyncio.CancelledError):
                await call
            while host.locks["child_a"].locked():  # noqa: ASYNC110
                await asyncio.sleep(0.01)

        host.children.lock("child_a", "test").release()
        assert provider.requests == []

    async def test_cancel_before_the_turn_starts_ends_it_without_a_request(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        parent = _parent(tmp_path)
        host = _host(parent)
        child, _ = host.build(host.definitions[0], session_id="child_a", description="d", background=False)
        outcomes: list[ChildOutcome] = []

        async def on_complete(outcome: ChildOutcome) -> None:
            outcomes.append(outcome)

        turn = host.start_turn(child, "go", session_id="child_a", description="d", on_complete=on_complete)
        turn.cancel()
        events = [event async for event in turn.events]

        assert turn.task.done()
        assert events == []
        assert provider.requests == []
        assert outcomes == [turn.outcome]
        assert turn.outcome is not None
        assert (turn.outcome.status, turn.outcome.error) == ("cancelled", "cancelled")

    def test_the_call_or_parent_effort_is_rounded_up_to_one_the_child_model_accepts(
        self, tmp_path: Path, provider: _Provider
    ) -> None:
        settings = _settings(
            tmp_path,
            providers={
                "other": ProviderConfig(name="other", type="openai", models={"plain": ModelConfig()}, api_key="k")
            },
        )
        store = SessionStore(data_dir=tmp_path / "sessions")

        def build(definition: AgentDefinition, effort: str | None = None) -> tuple[Agent, str]:
            return build_child_agent(
                store=store,
                settings=settings,
                # The parent's model accepts low and high.
                resolved_provider=_resolved("low"),
                parent_session_id="parent",
                review=None,
                definition=definition,
                session_id="child",
                description="d",
                background=False,
                effort=effort,
            )

        inherited, _ = build(AgentDefinition(name="a", description="d"))
        raised, _ = build(AgentDefinition(name="a", description="d"), effort="medium")
        other, other_provider = build(AgentDefinition(name="b", description="d", model="other/plain"), effort="high")

        assert (inherited.model, inherited.reasoning_effort) == ("gpt-5.5", "low")
        assert (raised.model, raised.reasoning_effort) == ("gpt-5.5", "high")
        assert (other_provider, other.model, other.reasoning_effort) == ("other", "plain", None)
        assert other.session_dir == tmp_path / "sessions" / "parent" / "agents"

    async def test_the_call_effort_reaches_the_child_request(self, tmp_path: Path, provider: _Provider) -> None:
        parent = _parent(tmp_path)
        provider.script("child_a", _text("one"))

        await _call_tool(parent, "call_a", effort="xhigh")

        assert provider.requests_for("child_a")[0].reasoning_effort == "high"
