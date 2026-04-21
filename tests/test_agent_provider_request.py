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
    return ToolExecutionResult(output=str(args.get("text") or ""))


PING_TOOL = ToolSpec(
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


async def capture_request(tmp_path: Path, **agent_kwargs):
    adapter = _CaptureAdapter()
    agent = Agent(
        model="gpt-5.4",
        provider="openai",
        cwd=str(tmp_path),
        session_dir=tmp_path / "session",
        **agent_kwargs,
    )

    with patch("mycode.agent.get_provider_adapter", return_value=adapter):
        _ = [event async for event in agent.achat("hello")]

    return adapter.requests[0]


@pytest.mark.asyncio
class TestAgentProviderRequest:
    async def test_includes_session_id(self, tmp_path: Path) -> None:
        request = await capture_request(tmp_path, session_id="session-explicit")

        assert request.session_id == "session-explicit"

    async def test_uses_explicit_system_prompt(self, tmp_path: Path) -> None:
        request = await capture_request(tmp_path, system="Use this exact system prompt.")

        assert request.system == "Use this exact system prompt."

    async def test_includes_custom_tool_definitions(self, tmp_path: Path) -> None:
        request = await capture_request(tmp_path, tools=[PING_TOOL])

        assert request.tools == [
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
