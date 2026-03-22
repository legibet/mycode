from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest

from mycode.core.agent import Agent
from mycode.core.providers.base import ProviderStreamEvent
from mycode.core.tools import ToolExecutor, ToolSpec


class _CaptureAdapter:
    def __init__(self) -> None:
        self.requests = []

    async def stream_turn(self, request) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        yield ProviderStreamEvent(
            "message_done", {"message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}
        )


class _CustomToolExecutor(ToolExecutor):
    def __init__(self, *, cwd: str, session_dir: Path) -> None:
        super().__init__(
            cwd=cwd,
            session_dir=session_dir,
            tools=(
                ToolSpec(
                    name="ping",
                    description="Echoes a short string.",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string", "description": "Text to echo."}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    method_name="ping",
                ),
            ),
        )

    def ping(self, *, text: str) -> str:
        return text


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

    with patch("mycode.core.agent.get_provider_adapter", return_value=adapter):
        _ = [event async for event in agent.achat("hello")]

    assert adapter.requests[0].session_id == "session-explicit"


@pytest.mark.asyncio
async def test_agent_falls_back_to_session_dir_name_for_provider_request(tmp_path: Path) -> None:
    adapter = _CaptureAdapter()
    session_dir = tmp_path / "session-derived"
    agent = Agent(
        model="gpt-5.4",
        provider="openai",
        cwd=str(tmp_path),
        session_dir=session_dir,
    )

    with patch("mycode.core.agent.get_provider_adapter", return_value=adapter):
        _ = [event async for event in agent.achat("hello")]

    assert adapter.requests[0].session_id == "session-derived"


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

    with patch("mycode.core.agent.get_provider_adapter", return_value=adapter):
        _ = [event async for event in agent.achat("hello")]

    assert adapter.requests[0].system == "Use this exact system prompt."


@pytest.mark.asyncio
async def test_agent_uses_tool_executor_definitions_in_provider_request(tmp_path: Path) -> None:
    adapter = _CaptureAdapter()
    session_dir = tmp_path / "session-custom-tools"
    agent = Agent(
        model="gpt-5.4",
        provider="openai",
        cwd=str(tmp_path),
        session_dir=session_dir,
        tool_executor=_CustomToolExecutor(cwd=str(tmp_path), session_dir=session_dir),
    )

    with patch("mycode.core.agent.get_provider_adapter", return_value=adapter):
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
