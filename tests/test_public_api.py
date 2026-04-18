"""Public SDK surface tests (Agent defaults, decorator, custom tools)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from mycode import (
    Agent,
    SessionStore,
    ToolContext,
    ToolExecutionResult,
    ToolExecutor,
    bash_tool,
    edit_tool,
    read_tool,
    text_block,
    tool,
    write_tool,
)
from mycode.providers.base import ProviderStreamEvent


class _CaptureAdapter:
    def __init__(self, text: str = "ok") -> None:
        self.requests = []
        self.message_snapshots = []
        self.text = text

    async def stream_turn(self, request) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        self.message_snapshots.append(deepcopy(request.messages))
        yield ProviderStreamEvent(
            "message_done",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": self.text}]}},
        )


class _ToolLoopAdapter:
    def __init__(self) -> None:
        self.requests = []

    async def stream_turn(self, request) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderStreamEvent(
                "message_done",
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "read_back",
                                "input": {"path": "note.txt"},
                            }
                        ],
                    }
                },
            )
            return

        yield ProviderStreamEvent(
            "message_done",
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}},
        )


@tool
def ping(text: str) -> str:
    """Echo text."""

    return f"pong: {text}"


@tool(streams_output=True)
def stream_ping(context: ToolContext, text: str) -> ToolExecutionResult:
    """Stream text back."""

    if context.emit:
        context.emit(f"stream: {text}")
    return ToolExecutionResult(model_text=f"done: {text}", display_text=f"done: {text}")


@tool
def read_back(context: ToolContext, path: str) -> str:
    """Read a file through the built-in read tool."""

    return context.call("read", {"path": path}).model_text


def _new_agent(tmp_path: Path, **overrides) -> Agent:
    """Build an Agent rooted under tmp_path so tests don't touch the real home."""

    session_id = overrides.pop("session_id", "session")
    overrides.setdefault("model", "gpt-5.4")
    overrides.setdefault("cwd", str(tmp_path))
    overrides.setdefault("session_dir", tmp_path / session_id)
    return Agent(**overrides)


def test_agent_starts_with_no_tools_by_default(tmp_path: Path) -> None:
    agent = _new_agent(tmp_path)
    assert agent.tools.definitions == []


def test_agent_registers_builtin_tools_when_requested(tmp_path: Path) -> None:
    agent = _new_agent(tmp_path, tools=[read_tool, write_tool, edit_tool, bash_tool])
    assert {t["name"] for t in agent.tools.definitions} == {"read", "write", "edit", "bash"}


def test_agent_infers_provider_from_known_model_prefix(tmp_path: Path) -> None:
    agent = _new_agent(tmp_path, model="claude-sonnet-4-6")
    assert agent.provider == "anthropic"


def test_agent_rejects_unknown_model_when_provider_omitted(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not infer provider"):
        _new_agent(tmp_path, model="not-a-real-model")


def test_agent_session_id_defaults_to_uuid(tmp_path: Path) -> None:
    agent = Agent(model="gpt-5.4", cwd=str(tmp_path))
    assert agent.session_id and len(agent.session_id) == 32
    assert agent.session_dir.name == agent.session_id


@pytest.mark.asyncio
async def test_agent_registers_custom_tools_in_provider_request(tmp_path: Path) -> None:
    agent = _new_agent(tmp_path, system="You are helpful.", tools=[ping, stream_ping])
    adapter = _CaptureAdapter()
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        _ = [ev async for ev in agent.achat({"role": "user", "content": [text_block("hi")]})]
    assert {t["name"] for t in adapter.requests[0].tools} == {"ping", "stream_ping"}


@pytest.mark.asyncio
async def test_agent_persists_messages_to_default_store(tmp_path: Path) -> None:
    """``achat`` writes every emitted message to the session log on disk."""

    agent = _new_agent(tmp_path, session_id="s1")
    with patch("mycode.agent.get_provider_adapter", return_value=_CaptureAdapter("first reply")):
        _ = [ev async for ev in agent.achat("first question")]

    store = SessionStore(data_dir=tmp_path)
    loaded = await store.load_session("s1")
    assert loaded is not None
    assert [m["role"] for m in loaded["messages"]] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_agent_resumes_existing_session_on_construction(tmp_path: Path) -> None:
    first = _new_agent(tmp_path, session_id="s2")
    with patch("mycode.agent.get_provider_adapter", return_value=_CaptureAdapter("first reply")):
        _ = [ev async for ev in first.achat("first question")]

    second = _new_agent(tmp_path, session_id="s2")
    assert [m["role"] for m in second.messages] == ["user", "assistant"]

    adapter = _CaptureAdapter("second reply")
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        _ = [ev async for ev in second.achat("second question")]

    assert len(adapter.message_snapshots[0]) == 3
    assert adapter.message_snapshots[0][0]["content"][0]["text"] == "first question"
    assert adapter.message_snapshots[0][1]["content"][0]["text"] == "first reply"
    assert adapter.message_snapshots[0][2]["content"][0]["text"] == "second question"


@pytest.mark.asyncio
async def test_custom_tool_can_reuse_builtin_tools(tmp_path: Path) -> None:
    note_path = tmp_path / "note.txt"
    note_path.write_text("hello from sdk\n", encoding="utf-8")

    agent = _new_agent(tmp_path, tools=[read_tool, read_back])
    adapter = _ToolLoopAdapter()
    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        events = [ev async for ev in agent.achat("Read note.txt and repeat it.")]

    assert [ev.type for ev in events] == ["tool_start", "tool_done"]
    assert events[1].data["model_text"] == "hello from sdk"


def test_tool_decorator_infers_schema_from_signature() -> None:
    @tool
    def lookup(key: str, limit: int = 10) -> str:
        """Find entries matching ``key``."""

        return f"{key}:{limit}"

    assert lookup.name == "lookup"
    assert lookup.description.startswith("Find entries")
    assert lookup.input_schema["properties"]["key"] == {"type": "string"}
    assert lookup.input_schema["properties"]["limit"] == {"type": "integer", "default": 10}
    assert lookup.input_schema["required"] == ["key"]
    assert lookup.streams_output is False


def test_tool_path_annotation_passes_path_instance(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    @tool
    def show(target: Path) -> str:
        """Echo the target path type and value."""

        captured["value"] = target
        return str(target)

    assert show.input_schema["properties"]["target"] == {"type": "string"}

    result = show.runner(
        ToolContext(executor=ToolExecutor(cwd=".", session_dir=tmp_path / "_p")),
        {"target": "/etc/hosts"},
    )
    assert isinstance(captured["value"], Path)
    assert captured["value"] == Path("/etc/hosts")
    assert result.model_text == "/etc/hosts"
