"""Tests for the interactive chat: input handling, attachments, and tool review."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import shlex
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast, override

import pytest
from conftest import ESC_WAIT, TerminalHarness
from prompt_toolkit.completion import Completer, DummyCompleter
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from mycode.agent import Agent, Event
from mycode.messages import ConversationMessage, merge_user_messages
from mycode.providers import list_env_discoverable_providers, provider_env_api_key_names
from mycode.providers.base import ProviderStreamEvent
from mycode.tools import ToolExecutor
from mycode_cli.config import PermissionConfig, Settings, WebConfig, get_settings
from mycode_cli.permissions import ToolReviewRequest
from mycode_cli.runtime import load_session_totals
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


def _chat(agent: object, tmp_path: Path, harness: TerminalHarness | None = None) -> TerminalChat:
    chat = TerminalChat(
        agent=cast(Any, agent),
        settings=settings_for(str(tmp_path)),
        store=SessionStore(data_dir=tmp_path / "sessions"),
        session_id="s",
    )
    if harness is not None:
        chat.terminal = harness.terminal
    return chat


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

        chat = _chat(_AttachmentAgent(), tmp_path)
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

        chat = _chat(_AttachmentAgent(), tmp_path)
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

        chat = _chat(_AttachmentAgent(**agent_kwargs), tmp_path)
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

        chat = _chat(_AttachmentAgent(supports_pdf_input=False), tmp_path)
        message = chat._build_user_message(f"check @{image_file} @{pdf_file}")

        # Order follows the prompt: image block first, then the PDF placeholder.
        assert message["content"][1]["type"] == "image"
        assert message["content"][2]["type"] == "text"
        assert 'kind="document">Current model does not support PDF input.' in message["content"][2]["text"]

    def test_skips_binary_attachment_that_is_not_image_or_pdf(self, tmp_path: Path, cli_home: Path) -> None:
        binary_file = tmp_path / "blob.bin"
        binary_file.write_bytes(b"\x00\x01\x02\xff\xfe")

        chat = _chat(_AttachmentAgent(), tmp_path)
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

        chat = _chat(_ReviewAgent(), tmp_path, harness)
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


class _TurnAgent(_AttachmentAgent):
    """Fake agent whose first turn waits for ``finish`` or a cancel, then delivers pending steers."""

    context_window = None

    def __init__(self, *, accepting: bool = True, fail_commit: bool = False) -> None:
        super().__init__()
        self.accepting = accepting
        self.fail_commit = fail_commit
        self.messages: list[ConversationMessage] = []
        self.steers: list[ConversationMessage] = []
        self.turns: list[ConversationMessage] = []
        self.cancels = 0
        self.finish = asyncio.Event()

    def steer(self, message: ConversationMessage) -> bool:
        if self.accepting:
            self.steers.append(message)
        return self.accepting

    def take_steers(self) -> list[ConversationMessage]:
        steers, self.steers = self.steers, []
        return steers

    def pending_steers(self) -> list[ConversationMessage]:
        return list(self.steers)

    @override
    def cancel(self) -> None:
        self.cancels += 1
        self.finish.set()

    async def achat(self, message: ConversationMessage, *, on_persist: object = None) -> AsyncIterator[Event]:
        self.turns.append(message)
        # The real achat() commits a stamped copy of its input before anything
        # else can stop it.
        if self.fail_commit and len(self.turns) > 1:
            raise OSError("disk full")
        self.messages.append({**message, "meta": {**(message.get("meta") or {}), "created_at": "2026-01-01T00:00:00Z"}})
        if len(self.turns) == 1:
            await self.finish.wait()
        if self.cancels:
            yield Event("cancelled", {})
            return
        if steers := self.take_steers():
            delivered = merge_user_messages(steers, steer=True)
            self.messages.append(delivered)
            yield Event("user_message", {"message": delivered})
        yield Event("text", {"delta": f"answer {len(self.turns)}"})


def _steered(agent: _TurnAgent) -> list[ConversationMessage]:
    return [message for message in agent.messages if message["meta"].get("steer")]


class TestMidTurnInput:
    """Enter steers and Ctrl+Q queues while a turn runs; Alt+Up and Esc take the pending input back."""

    @staticmethod
    async def _turn(
        harness: TerminalHarness,
        tmp_path: Path,
        agent: _TurnAgent,
        keys: list[str],
        *,
        finish: bool = True,
        raises: type[Exception] | None = None,
        after: list[str] | None = None,
    ) -> tuple[TerminalChat, list[str]]:
        """Run one turn while typing ``keys``, let it finish, then submit ``after`` and collect what is read."""

        chat = _chat(agent, tmp_path, harness)
        read: list[str] = []

        async def main() -> None:
            turn = asyncio.create_task(chat._run_turn("first"))
            for key in keys:
                await asyncio.sleep(0.1)
                harness.pipe.send_text(key)
            await asyncio.sleep(0.3)
            if finish:
                agent.finish.set()
            if raises is None:
                await turn
            else:
                with pytest.raises(raises):
                    await turn
            for key in after or []:
                harness.pipe.send_text(key)
                read.append(await harness.terminal.read())

        await harness.terminal.run(main)
        return chat, read

    async def test_enter_steers_with_the_built_message(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path
    ) -> None:
        (tmp_path / "notes.txt").write_text("remember sqlite", encoding="utf-8")
        agent = _TurnAgent()

        chat, _ = await self._turn(harness, tmp_path, agent, ["use @notes.txt\r"])

        [steer] = _steered(agent)
        assert steer["content"] == chat._build_user_message("use @notes.txt")["content"]
        assert "remember sqlite" in steer["content"][1]["text"]
        assert len(steer["meta"]["input_ids"]) == 1
        assert chat._steers == []
        rendered = harness.text()
        assert "steer use @notes.txt" in rendered
        assert "❯ use @notes.txt  steer" in rendered
        assert len(agent.turns) == 1

    async def test_refused_steer_and_ctrl_q_queue_for_the_next_turn(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path
    ) -> None:
        agent = _TurnAgent(accepting=False)

        await self._turn(harness, tmp_path, agent, ["one\r", "two\x11"])

        merged = agent.turns[1]
        assert merged["content"] == [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]
        assert "steer" not in merged["meta"]
        assert len(set(merged["meta"]["input_ids"])) == 2
        rendered = harness.text()
        assert "queued one" in rendered
        assert "queued two" in rendered
        assert "❯ one\n\n  two\n" in rendered
        assert "answer 2" in rendered

    async def test_ctrl_q_queues_even_when_steering_works(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path
    ) -> None:
        agent = _TurnAgent()

        await self._turn(harness, tmp_path, agent, ["later\x11"])

        assert _steered(agent) == []
        assert [block["text"] for block in agent.turns[1]["content"]] == ["later"]

    @pytest.mark.parametrize("command", ["/model", "exit", "quit"])
    async def test_commands_are_refused_during_a_turn_and_stay_in_the_input(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path, command: str
    ) -> None:
        agent = _TurnAgent()

        _, read = await self._turn(harness, tmp_path, agent, [f"{command}\r"], after=["\r"])

        assert _steered(agent) == []
        assert len(agent.turns) == 1
        assert "commands are unavailable during a turn" in harness.text()
        assert read == [command]

    async def test_compact_with_text_is_sent_as_a_steer(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path
    ) -> None:
        agent = _TurnAgent()

        await self._turn(harness, tmp_path, agent, ["/compact some text\r"])

        [steer] = _steered(agent)
        assert steer["content"] == [{"type": "text", "text": "/compact some text"}]
        assert "commands are unavailable during a turn" not in harness.text()

    async def test_alt_up_takes_back_steers_and_queue_ahead_of_the_draft(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path
    ) -> None:
        agent = _TurnAgent()

        chat, read = await self._turn(harness, tmp_path, agent, ["a\r", "b\x11", "draft", "\x1b[1;3A"], after=["\r"])

        assert (chat._steers, chat._queue) == ([], [])
        assert len(agent.turns) == 1
        assert read == ["a\n\nb\n\ndraft"]

    async def test_alt_up_after_the_agent_took_a_steer_leaves_it_to_its_delivery(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path
    ) -> None:
        class DeliveringAgent(_TurnAgent):
            def __init__(self) -> None:
                super().__init__()
                self.taken = asyncio.Event()
                self.deliver = asyncio.Event()

            @override
            def take_steers(self) -> list[ConversationMessage]:
                steers = super().take_steers()
                if steers:
                    self.taken.set()
                return steers

            @override
            async def achat(self, message: ConversationMessage, *, on_persist: object = None) -> AsyncIterator[Event]:
                async for event in super().achat(message, on_persist=on_persist):
                    if event.type == "user_message":
                        await self.deliver.wait()
                    yield event

        agent = DeliveringAgent()
        chat = _chat(agent, tmp_path, harness)
        pending: list[list[str]] = []
        read: list[str] = []

        async def main() -> None:
            turn = asyncio.create_task(chat._run_turn("first"))
            await asyncio.sleep(0.1)
            harness.pipe.send_text("a\r")
            await asyncio.sleep(0.1)
            harness.pipe.send_text("draft")
            await asyncio.sleep(0.1)
            agent.finish.set()
            await asyncio.wait_for(agent.taken.wait(), 2)
            harness.pipe.send_text("\x1b[1;3A")
            await asyncio.sleep(0.3)
            pending.append([line.plain for line in harness.terminal._pending_input])
            agent.deliver.set()
            await turn
            pending.append([line.plain for line in harness.terminal._pending_input])
            harness.pipe.send_text("\r")
            read.append(await harness.terminal.read())

        await harness.terminal.run(main)

        assert pending == [["steer a"], []]
        assert read == ["draft"]
        assert [steer["content"] for steer in _steered(agent)] == [[{"type": "text", "text": "a"}]]
        assert harness.text().count("❯ a  steer") == 1

    async def test_error_returns_the_steer_then_the_queue_ahead_of_the_draft(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path
    ) -> None:
        class FailingAgent(_TurnAgent):
            @override
            async def achat(self, message: ConversationMessage, *, on_persist: object = None) -> AsyncIterator[Event]:
                self.turns.append(message)
                self.messages.append(message)
                await self.finish.wait()
                yield Event("error", {"message": "provider failed"})

        agent = FailingAgent()

        chat, read = await self._turn(harness, tmp_path, agent, ["a\r", "b\x11", "draft"], after=["\r"])

        assert (chat._steers, chat._queue) == ([], [])
        assert len(agent.turns) == 1
        assert read == ["a\n\nb\n\ndraft"]

    async def test_esc_takes_back_then_cancels(self, harness: TerminalHarness, tmp_path: Path, cli_home: Path) -> None:
        agent = _TurnAgent()

        _, read = await self._turn(harness, tmp_path, agent, ["a\r", "b\x11", "\x1b"], finish=False, after=["\r"])

        assert agent.cancels == 1
        assert len(agent.turns) == 1
        assert "cancelled" in harness.text()
        assert read == ["a\n\nb"]

    async def test_a_queued_turn_whose_commit_fails_returns_to_the_editor(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path
    ) -> None:
        agent = _TurnAgent(fail_commit=True)

        chat, read = await self._turn(harness, tmp_path, agent, ["later\x11"], raises=OSError, after=["\r"])

        assert len(agent.turns) == 2
        assert chat._queue == []
        assert read == ["later"]

    async def test_a_committed_turn_is_not_returned_to_the_editor(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real SDK commits a stamped copy of the input, not the object the TUI built."""

        release = asyncio.Event()

        class Adapter:
            supports_reasoning_effort = False
            calls = 0

            async def stream_turn(self, request: object) -> AsyncIterator[ProviderStreamEvent]:
                Adapter.calls += 1
                if Adapter.calls == 1:
                    await release.wait()
                yield ProviderStreamEvent(
                    "message_done", {"message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}
                )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr("mycode.agent.get_provider_adapter", lambda _provider: Adapter())
        agent = Agent(
            model="claude-sonnet-4-6", provider="anthropic", session_dir=tmp_path / "sessions", session_id="s"
        )
        chat = _chat(agent, tmp_path, harness)
        read: list[str] = []

        async def main() -> None:
            turn = asyncio.create_task(chat._run_turn("first"))
            await asyncio.sleep(0.1)
            harness.pipe.send_text("later\x11")
            await asyncio.sleep(0.2)
            release.set()
            await turn
            harness.pipe.send_text("draft\r")
            read.append(await harness.terminal.read())

        await harness.terminal.run(main)

        assert [m["role"] for m in agent.messages] == ["user", "assistant", "user", "assistant"]
        assert agent.messages[2]["meta"]["input_ids"]
        assert read == ["draft"]

    async def test_esc_while_the_queued_turn_is_prepared_returns_the_queue(
        self, harness: TerminalHarness, tmp_path: Path, cli_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _TurnAgent()
        preparing = asyncio.Event()
        release = asyncio.Event()

        async def gated(store: SessionStore, session_id: str) -> Any:
            if preparing.is_set():
                await release.wait()
            preparing.set()
            return await load_session_totals(store, session_id)

        monkeypatch.setattr("mycode_cli.tui.chat.load_session_totals", gated)
        chat = _chat(agent, tmp_path, harness)
        read: list[str] = []

        async def main() -> None:
            turn = asyncio.create_task(chat._run_turn("first"))
            await asyncio.sleep(0.1)
            harness.pipe.send_text("later\x11")
            await asyncio.sleep(0.1)
            agent.finish.set()
            await asyncio.sleep(0.2)
            # The first turn is over and the queued turn is being prepared.
            harness.pipe.send_text("\x1b")
            await asyncio.sleep(ESC_WAIT)
            release.set()
            await turn
            harness.pipe.send_text("\r")
            read.append(await harness.terminal.read())

        await harness.terminal.run(main)

        assert len(agent.turns) == 1
        assert chat._queue == []
        assert read == ["later"]
        assert "answer 2" not in harness.text()


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
