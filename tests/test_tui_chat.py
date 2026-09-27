"""Tests for the interactive chat: input handling, attachments, and tool review."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import shlex
from pathlib import Path
from typing import Any, cast, override

import pytest
from conftest import TerminalHarness
from prompt_toolkit.completion import Completer, DummyCompleter
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from mycode.agent import Agent
from mycode.providers import list_env_discoverable_providers, provider_env_api_key_names
from mycode.tools import ToolExecutor
from mycode_cli.config import PermissionConfig, Settings, WebConfig, get_settings
from mycode_cli.permissions import ToolReviewRequest
from mycode_cli.sessions import SessionStore
from mycode_cli.tools import DEFAULT_TOOLS
from mycode_cli.tui.chat import (
    TerminalChat,
    _build_chat_key_bindings,
    _PromptCompleter,
    clone_agent,
)
from mycode_cli.tui.terminal import Terminal
from mycode_cli.tui.theme import TOOL_MARKER
from mycode_cli.web_tools import build_web_tools
from mycode_cli.workspace import CliDeps


def settings_for(cwd: str) -> Settings:
    return Settings(
        providers={},
        port=8000,
        cwd=cwd,
        project=cwd,
        config_paths=[],
    )


class _AttachmentAgent:
    provider = "anthropic"
    model = "claude-sonnet-4-6"

    def __init__(
        self,
        *,
        supports_image_input: bool = True,
        supports_pdf_input: bool = True,
    ) -> None:
        self.supports_image_input = supports_image_input
        self.supports_pdf_input = supports_pdf_input
        self.tools = ToolExecutor(DEFAULT_TOOLS)

    def cancel(self) -> None:
        return None


def test_clone_agent_keeps_configured_tools_and_uses_the_new_session_directory(tmp_path: Path) -> None:
    store = SessionStore(data_dir=tmp_path / "sessions")
    agent = Agent(
        model="gpt-5.5",
        provider="openai",
        session_dir=store.data_dir,
        session_id="old",
        tools=build_web_tools(WebConfig(search="tavily")),
    )

    cloned = clone_agent(agent, store=store, session_id="new", cwd=str(tmp_path))

    assert cloned.tools.specs == agent.tools.specs
    assert cloned.deps == CliDeps.for_session(cwd=tmp_path, data_dir=store.data_dir, session_id="new")


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".mycode"
    monkeypatch.setenv("MYCODE_HOME", str(home))
    return home


async def _submit(tmp_path: Path, steps: list[str | bytes], *, completer: Completer | None = None) -> str:
    """Drive the chat input with key steps and return the first submitted text."""

    with create_pipe_input() as pipe:
        terminal = Terminal(
            history_path=str(tmp_path / "history"),
            completer=completer or DummyCompleter(),
            key_bindings=_build_chat_key_bindings(),
            input=pipe,
            output=DummyOutput(),
        )
        results: list[str] = []

        async def main() -> None:
            async def drive_input() -> None:
                for step in steps:
                    await asyncio.sleep(0.1)
                    if isinstance(step, bytes):
                        pipe.send_bytes(step)
                    else:
                        pipe.send_text(step)

            task = asyncio.create_task(drive_input())
            results.append(await terminal.read())
            await task

        await terminal.run(main)
    return results[0]


class TestPromptInput:
    async def test_enter_submits_unique_slash_completion(self, tmp_path: Path) -> None:
        result = await _submit(tmp_path, ["/m", "\t", "\r"], completer=_PromptCompleter())

        assert result == "/model"

    async def test_enter_accepts_ambiguous_slash_completion_before_submit(self, tmp_path: Path) -> None:
        result = await _submit(tmp_path, ["/r", "\t", "\r", "\r"], completer=_PromptCompleter())

        assert result == "/resume"

    async def test_enter_accepts_path_completion(self, tmp_path: Path) -> None:
        (tmp_path / "folder").mkdir()
        (tmp_path / "folder" / "bar.txt").write_text("x", encoding="utf-8")

        result = await _submit(tmp_path, ["@f", "\t", "\r", "\r", "\r"], completer=_PromptCompleter(cwd=str(tmp_path)))

        assert result == "@folder/bar.txt"

    async def test_bracketed_paste_rewrites_existing_paths(self, tmp_path: Path) -> None:
        image_a = tmp_path / "a.png"
        image_b = tmp_path / "b b.jpg"
        note = tmp_path / "note.txt"
        image_a.write_bytes(b"x")
        image_b.write_bytes(b"x")
        note.write_text("x", encoding="utf-8")

        async def paste(pasted: str) -> str:
            return await _submit(tmp_path, [b"\x1b[200~" + pasted.encode() + b"\x1b[201~", "\r"])

        assert await paste(str(image_a)) == f"@{image_a}"
        assert await paste(f'"{image_b}"') == f"@'{image_b}'"
        assert await paste(f"{image_a} '{image_b}'") == f"@{image_a} @'{image_b}'"
        assert await paste(str(note)) == f"@{note}"

    async def test_bracketed_paste_keeps_non_file_text_unchanged(self, tmp_path: Path) -> None:
        result = await _submit(tmp_path, [b"\x1b[200~hello world\x1b[201~", "\r"])

        assert result == "hello world"


class TestAttachments:
    def test_builds_message_with_text_and_image_attachments(self, tmp_path: Path, cli_home: Path) -> None:
        code_file = tmp_path / "main.py"
        image_file = tmp_path / "diagram.png"
        code_file.write_text("print('hello')\n", encoding="utf-8")
        image_file.write_bytes(b"\x89PNG\r\n\x1a\nrest")

        chat = TerminalChat(
            agent=cast(Any, _AttachmentAgent()),
            settings=settings_for(str(tmp_path)),
            store=cast(Any, object()),
            session_id="test-session",
        )
        message = chat._build_user_message(f"check @{code_file} @{image_file}")

        assert message["role"] == "user"
        assert message["content"][0] == {"type": "text", "text": f"check @{code_file} @{image_file}"}
        assert message["content"][1]["type"] == "text"
        assert message["content"][1]["meta"] == {"attachment": True, "path": str(code_file)}
        assert "print('hello')" in message["content"][1]["text"]
        assert message["content"][2] == {
            "type": "image",
            "data": base64.b64encode(image_file.read_bytes()).decode("utf-8"),
            "mime_type": "image/png",
            "name": "diagram.png",
        }

    def test_builds_message_with_pdf_attachment(self, tmp_path: Path, cli_home: Path) -> None:
        pdf_file = tmp_path / "report.pdf"
        pdf_file.write_bytes(b"%PDF-1.7\nrest")

        chat = TerminalChat(
            agent=cast(Any, _AttachmentAgent()),
            settings=settings_for(str(tmp_path)),
            store=cast(Any, object()),
            session_id="test-session",
        )
        message = chat._build_user_message(f"summarize @{pdf_file}")

        assert message["role"] == "user"
        assert message["content"][0] == {"type": "text", "text": f"summarize @{pdf_file}"}
        assert message["content"][1] == {
            "type": "document",
            "data": base64.b64encode(pdf_file.read_bytes()).decode("utf-8"),
            "mime_type": "application/pdf",
            "name": "report.pdf",
        }

    @pytest.mark.parametrize(
        ("filename", "payload", "agent_kwargs", "expected_text"),
        [
            (
                "diagram.png",
                b"\x89PNG\r\n\x1a\nrest",
                {"supports_image_input": False},
                'media_type="image/png" kind="image">Current model does not support image input.',
            ),
            (
                'report <"draft">.pdf',
                b"%PDF-1.7\nrest",
                {"supports_pdf_input": False},
                'media_type="application/pdf" kind="document">Current model does not support PDF input.',
            ),
        ],
    )
    def test_falls_back_to_text_notice_for_unsupported_media(
        self,
        tmp_path: Path,
        cli_home: Path,
        filename: str,
        payload: bytes,
        agent_kwargs: dict[str, bool],
        expected_text: str,
    ) -> None:
        path = tmp_path / filename
        path.write_bytes(payload)

        chat = TerminalChat(
            agent=cast(
                Any,
                _AttachmentAgent(**agent_kwargs),
            ),
            settings=settings_for(str(tmp_path)),
            store=cast(Any, object()),
            session_id="test-session",
        )
        message = chat._build_user_message(f"check @{shlex.quote(str(path))}")

        assert message["role"] == "user"
        assert message["content"][0] == {"type": "text", "text": f"check @{shlex.quote(str(path))}"}
        assert message["content"][1] == {
            "type": "text",
            "text": f'<file name="{html.escape(str(path), quote=True)}" {expected_text}</file>',
            "meta": {"attachment": True, "path": str(path)},
        }

    def test_keeps_attachment_input_order_when_mixing_supported_and_placeholder(
        self, tmp_path: Path, cli_home: Path
    ) -> None:
        image_file = tmp_path / "diagram.png"
        pdf_file = tmp_path / "report.pdf"
        image_file.write_bytes(b"\x89PNG\r\n\x1a\nrest")
        pdf_file.write_bytes(b"%PDF-1.7\nrest")

        chat = TerminalChat(
            agent=cast(
                Any,
                _AttachmentAgent(supports_pdf_input=False),
            ),
            settings=settings_for(str(tmp_path)),
            store=cast(Any, object()),
            session_id="test-session",
        )
        message = chat._build_user_message(f"check @{image_file} @{pdf_file}")

        # Order follows the prompt: image block first, then the PDF placeholder.
        assert message["content"][1]["type"] == "image"
        assert message["content"][2]["type"] == "text"
        assert 'kind="document">Current model does not support PDF input.' in message["content"][2]["text"]

    def test_skips_binary_attachment_that_is_not_image_or_pdf(self, tmp_path: Path, cli_home: Path) -> None:
        binary_file = tmp_path / "blob.bin"
        binary_file.write_bytes(b"\x00\x01\x02\xff\xfe")

        chat = TerminalChat(
            agent=cast(Any, _AttachmentAgent()),
            settings=settings_for(str(tmp_path)),
            store=cast(Any, object()),
            session_id="test-session",
        )
        message = chat._build_user_message(f"check @{binary_file}")

        assert len(message["content"]) == 1
        assert message["content"][0]["type"] == "text"


class TestToolReview:
    @pytest.mark.parametrize(
        ("keys", "decision", "cancelled"),
        [
            pytest.param(["\r"], "allow", False, id="allow"),
            pytest.param(["\x1b[B", "\r"], "deny", True, id="deny"),
            pytest.param(["\x1b"], "deny", True, id="dismiss"),
        ],
    )
    async def test_review_prints_the_request_and_uses_the_chooser(
        self,
        harness: TerminalHarness,
        tmp_path: Path,
        cli_home: Path,
        keys: list[str],
        decision: str,
        cancelled: bool,
    ) -> None:
        cancels: list[str] = []

        class _ReviewAgent(_AttachmentAgent):
            @override
            def cancel(self) -> None:
                cancels.append("cancel")

        chat = TerminalChat(
            agent=cast(Any, _ReviewAgent()),
            settings=settings_for(str(tmp_path)),
            store=cast(Any, object()),
            session_id="test-session",
        )
        chat.terminal = harness.terminal
        results: list[str] = []

        async def main() -> None:
            async def drive_input() -> None:
                for key in keys:
                    await asyncio.sleep(0.1)
                    harness.pipe.send_text(key)

            task = asyncio.create_task(drive_input())
            request = ToolReviewRequest("call-1", "bash", "rm -rf build", PermissionConfig())
            results.append(await chat._review_tool_call(request))
            await task

        await chat.terminal.run(main)

        rendered = harness.text()
        assert results == [decision]
        assert cancels == (["cancel"] if cancelled else [])
        assert f"{TOOL_MARKER} Review  Bash\n  rm -rf build\n" in rendered


class TestModelSwitch:
    @pytest.fixture
    def chat(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> TerminalChat:
        for provider in list_env_discoverable_providers():
            for env_name in provider_env_api_key_names(provider):
                monkeypatch.delenv(env_name, raising=False)
        cli_home.mkdir(parents=True)
        providers = {
            "alpha": {
                "type": "openai_chat",
                "api_key": "k",
                "base_url": "http://alpha",
                "models": {"a1": {}, "a2": {}},
            },
            "beta": {"type": "openai_chat", "api_key": "k", "base_url": "http://beta", "models": {"b1": {}}},
        }
        (cli_home / "config.json").write_text(json.dumps({"providers": providers}))
        store = SessionStore(data_dir=tmp_path / "sessions")
        agent = Agent(model="a1", provider="openai_chat", session_dir=store.data_dir, session_id="s")
        chat = TerminalChat(
            agent=agent,
            settings=get_settings(str(tmp_path)),
            store=store,
            session_id="s",
            provider_name="alpha",
        )
        chat.terminal = harness.terminal
        return chat

    @pytest.mark.parametrize(
        ("query", "keys", "expected"),
        [
            pytest.param("", ["\x1b[B", "\x1b[B", "\r"], ("beta", "b1", "http://beta"), id="every-provider"),
            # Esc would cancel a picker, so the switch proves none opened.
            pytest.param("a2", ["\x1b"], ("alpha", "a2", "http://alpha"), id="exact-name"),
            pytest.param("bet", ["\r"], ("beta", "b1", "http://beta"), id="filtered-picker"),
        ],
    )
    async def test_switch_model(
        self,
        chat: TerminalChat,
        harness: TerminalHarness,
        query: str,
        keys: list[str],
        expected: tuple[str, str, str],
    ) -> None:
        async def main() -> None:
            async def send() -> None:
                for key in keys:
                    await asyncio.sleep(0.1)
                    harness.pipe.send_text(key)

            task = asyncio.create_task(send())
            await chat._switch_model(query)
            await task

        await chat.terminal.run(main)

        assert (chat.provider_name, chat.agent.model, chat.agent.api_base) == expected
