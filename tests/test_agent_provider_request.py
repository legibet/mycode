from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest

from mycode.agent import Agent
from mycode.providers.base import ProviderStreamEvent
from mycode.tools import ToolContext, ToolExecutionResult, ToolSpec


class _CaptureAdapter:
    def __init__(self) -> None:
        self.requests = []

    async def stream_turn(self, request) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        yield ProviderStreamEvent(
            "message_done", {"message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}
        )


def _ping_runner(_ctx: ToolContext, args: dict[str, object]) -> ToolExecutionResult:
    text = str(args.get("text") or "")
    return ToolExecutionResult(output=text)


_PING_TOOL = ToolSpec(
    name="ping",
    description="Echoes a short string.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to echo."}},
        "required": ["text"],
        "additionalProperties": False,
    },
    runner=_ping_runner,
)


@pytest.mark.asyncio
async def test_agent_passes_session_id_to_provider_request(tmp_path: Path) -> None:
    adapter = _CaptureAdapter()
    agent = Agent(
        model="gpt-5.4",
        provider="openai",
        cwd=str(tmp_path),
        session_dir=tmp_path / "session-explicit",
        session_id="session-explicit",
    )

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        _ = [event async for event in agent.achat("hello")]

    assert adapter.requests[0].session_id == "session-explicit"


@pytest.mark.asyncio
async def test_agent_uses_explicit_system_prompt_when_provided(tmp_path: Path) -> None:
    adapter = _CaptureAdapter()
    agent = Agent(
        model="gpt-5.4",
        provider="openai",
        cwd=str(tmp_path),
        session_dir=tmp_path / "session-explicit-system",
        system="Use this exact system prompt.",
    )

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        _ = [event async for event in agent.achat("hello")]

    assert adapter.requests[0].system == "Use this exact system prompt."


@pytest.mark.asyncio
async def test_agent_uses_tool_definitions_in_provider_request(tmp_path: Path) -> None:
    adapter = _CaptureAdapter()
    session_dir = tmp_path / "session-custom-tools"
    agent = Agent(
        model="gpt-5.4",
        provider="openai",
        cwd=str(tmp_path),
        session_dir=session_dir,
        tools=[_PING_TOOL],
    )

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        _ = [event async for event in agent.achat("hello")]

    assert adapter.requests[0].tools == [
        {
            "name": "ping",
            "description": "Echoes a short string.",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Text to echo."}},
                "required": ["text"],
                "additionalProperties": False,
            },
        }
    ]
