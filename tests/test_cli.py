"""Tests for CLI runtime and terminal behavior."""

from __future__ import annotations

import asyncio
import base64
import html
import shlex
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from mycode.agent import Event
from mycode.session import SessionStore
from mycode.tools import ToolExecutor
from mycode_cli.config import Settings
from mycode_cli.main import app, resolve_session, run_noninteractive
from mycode_cli.permissions import PERMISSION_DENIED_BY_USER_OUTPUT, PERMISSION_DENIED_OUTPUT
from mycode_cli.runtime import load_session_cost
from mycode_cli.tools import DEFAULT_TOOLS
from mycode_cli.tui.chat import (
    TerminalChat,
    _build_chat_key_bindings,
    _PromptCompleter,
)
from mycode_cli.tui.render import ReplyRenderer, TerminalView


def settings_for(cwd: str) -> Settings:
    return Settings(
        providers={},
        default_provider=None,
        default_model=None,
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
        cwd: str,
        supports_image_input: bool = True,
        supports_pdf_input: bool = True,
    ) -> None:
        self.cwd = cwd
        self.supports_image_input = supports_image_input
        self.supports_pdf_input = supports_pdf_input
        self.tools = ToolExecutor(DEFAULT_TOOLS)

    def cancel(self) -> None:
        return None


class _FakeAgent:
    provider = "anthropic"
    model = "claude-sonnet-4-6"
    cwd = "/tmp"
    api_base = None

    async def achat(self, message: str, *, on_persist=None):
        if on_persist:
            await on_persist({"role": "user", "content": [{"type": "text", "text": message}]})
            await on_persist(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Persisted final answer"}],
                }
            )
        yield Event("reasoning", {"delta": "Hidden reasoning"})
        yield Event("text", {"delta": "Streamed answer should stay hidden"})


class _ErrorAgent:
    async def achat(self, message: str, *, on_persist=None):
        yield Event("error", {"message": "provider error"})


class _PermissionDeniedAgent:
    def __init__(self, output: str) -> None:
        self.output = output

    async def achat(self, message: str, *, on_persist=None):
        yield Event("tool_done", {"tool_use_id": "call-1", "output": self.output, "is_error": True})


class _PermissionDeniedThenReplyAgent:
    async def achat(self, message: str, *, on_persist=None):
        yield Event("tool_done", {"tool_use_id": "call-1", "output": PERMISSION_DENIED_OUTPUT, "is_error": True})
        if on_persist:
            await on_persist(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Permission was denied. Use --permission standard."}],
                }
            )


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".mycode"
    monkeypatch.setenv("MYCODE_HOME", str(home))
    return home


class TestRunNoninteractive:
    @pytest.mark.asyncio
    async def test_prints_only_final_reply(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = await run_noninteractive(cast(Any, _FakeAgent()), "hello")

        captured = capsys.readouterr()
        assert code == 0
        assert captured.out == "Persisted final answer\n"
        assert captured.err == ""

    @pytest.mark.asyncio
    async def test_prints_errors_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = await run_noninteractive(cast(Any, _ErrorAgent()), "hello")

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert captured.err == "provider error\n"

    @pytest.mark.parametrize("output", [PERMISSION_DENIED_OUTPUT, PERMISSION_DENIED_BY_USER_OUTPUT])
    async def test_prints_permission_denials_to_stderr(self, output: str, capsys: pytest.CaptureFixture[str]) -> None:
        code = await run_noninteractive(cast(Any, _PermissionDeniedAgent(output)), "hello")

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert captured.err == f"{output}\n"

    @pytest.mark.asyncio
    async def test_prints_final_reply_after_permission_denial(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = await run_noninteractive(cast(Any, _PermissionDeniedThenReplyAgent()), "hello")

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == "Permission was denied. Use --permission standard.\n"
        assert captured.err == ""


@pytest.mark.asyncio
class TestResolveSession:
    async def test_defaults_to_new_session(self, tmp_path: Path) -> None:
        store = SessionStore(data_dir=tmp_path / "sessions")

        resolved = await resolve_session(
            store=store,
            cwd=str(tmp_path),
            requested_session_id=None,
            continue_last=False,
        )

        assert resolved.mode == "new"
        assert resolved.messages == []
        assert resolved.session_id
        assert await store.list_sessions() == []

    async def test_continue_reuses_latest_session(self, tmp_path: Path) -> None:
        store = SessionStore(data_dir=tmp_path / "sessions")
        await store.create_session("first", cwd=str(tmp_path))
        await store.create_session("second", cwd=str(tmp_path))
        await store.append_message(
            "second",
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        )

        resolved = await resolve_session(
            store=store,
            cwd=str(tmp_path),
            requested_session_id=None,
            continue_last=True,
        )

        assert resolved.mode == "resumed"
        assert resolved.session_id == "second"
        assert resolved.messages[0]["content"] == [{"type": "text", "text": "hello"}]

    async def test_explicit_missing_session_errors(self, tmp_path: Path) -> None:
        store = SessionStore(data_dir=tmp_path / "sessions")

        with pytest.raises(ValueError, match="Unknown session"):
            await resolve_session(
                store=store,
                cwd=str(tmp_path),
                requested_session_id="missing",
                continue_last=False,
            )


def test_print_history_preview_renders_recent_turns() -> None:
    output = StringIO()
    view = TerminalView(Console(file=output, force_terminal=False, color_system=None, width=120))

    view.print_history_preview(
        [
            {"role": "user", "content": [{"type": "text", "text": "older question"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "older answer"}]},
            {"role": "user", "content": [{"type": "text", "text": "turn one"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "answer one"}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "check @main.py"},
                    {
                        "type": "text",
                        "text": '<file name="/tmp/main.py">\nprint(1)\n</file>',
                        "meta": {"attachment": True, "path": "/tmp/main.py"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "hidden"},
                    {"type": "text", "text": "checking `foo`"},
                    {"type": "tool_use", "name": "read", "input": {"path": "foo.py"}},
                    {"type": "text", "text": "```py\nprint(1)\n```"},
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "latest question"}]},
        ]
    )

    rendered = output.getvalue()
    assert "older question" not in rendered
    assert "older answer" not in rendered
    assert '<file name="/tmp/main.py">' not in rendered
    assert "turn one" in rendered
    assert "check @main.py" in rendered
    assert "checking foo" in rendered
    assert "Read  foo.py" in rendered
    assert "print(1)" in rendered
    assert "latest question" in rendered


class TestReplyRenderer:
    def test_finish_keeps_streamed_text_visible(self) -> None:
        output = StringIO()
        renderer = ReplyRenderer(
            Console(file=output, force_terminal=False, color_system=None, width=120),
            model="m",
            context_window=None,
        )
        renderer.text("final answer")

        renderer.finish()

        assert "final answer" in output.getvalue()

    def test_tool_output_appends_text_deltas(self) -> None:
        output = StringIO()
        renderer = ReplyRenderer(
            Console(file=output, force_terminal=False, color_system=None, width=120),
            model="m",
            context_window=None,
        )
        renderer.tool_start("bash", {"command": "printf"})

        renderer.tool_output("one\nsec")
        renderer.tool_output("ond\n")
        renderer.tool_done("one\nsecond", is_error=False)

        assert "one\n    second" in output.getvalue()

    def test_tool_done_shows_final_status_after_live_output(self) -> None:
        output = StringIO()
        renderer = ReplyRenderer(
            Console(file=output, force_terminal=False, color_system=None, width=120),
            model="m",
            context_window=None,
        )
        renderer.tool_start("bash", {"command": "build"})
        renderer.tool_output("started\n")

        renderer.tool_done(
            "started\n\n[Output truncated: Showing the last 50KB of output. Full output: /tmp/bash.log.]"
            + "\n\n[Command timed out after 1s]",
            is_error=True,
        )

        rendered = output.getvalue()
        assert "Full output: /tmp/bash.log" in rendered
        assert "Command timed out after 1s" in rendered

    def test_finish_prints_context_and_session_cost(self) -> None:
        output = StringIO()
        renderer = ReplyRenderer(
            Console(file=output, force_terminal=False, color_system=None, width=120),
            model="gpt-5.5",
            context_window=128_000,
            session_cost_base=0.40,
        )
        renderer._stats = {
            "context_tokens": 34_210,
            "turn_cost": {"total": 0.02},
        }

        renderer.finish()

        assert "gpt-5.5  34,210 tokens (27%) · $0.42" in output.getvalue()

    @pytest.mark.parametrize(
        ("session_cost_base", "turn_cost", "expected"),
        [
            pytest.param(None, 0.02, 0.02, id="turn-only"),
            pytest.param(0.40, None, 0.40, id="history-only"),
            pytest.param(None, None, None, id="all-unknown"),
        ],
    )
    def test_finish_combines_known_costs(
        self, session_cost_base: float | None, turn_cost: float | None, expected: float | None
    ) -> None:
        output = StringIO()
        renderer = ReplyRenderer(
            Console(file=output, force_terminal=False, color_system=None, width=120),
            model="m",
            context_window=1_000,
            session_cost_base=session_cost_base,
        )
        renderer._stats = {"turn_cost": {"total": turn_cost} if turn_cost is not None else None}

        renderer.finish()

        rendered = output.getvalue()
        if expected is None:
            assert "$" not in rendered
        else:
            assert f"${expected:.2f}" in rendered


class TestLoadSessionCost:
    @pytest.mark.asyncio
    async def test_folds_the_raw_timeline_including_rewound_turns(self, tmp_path: Path) -> None:
        store = SessionStore(data_dir=tmp_path)
        await store.create_session("s1", cwd="/tmp")
        records = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "assistant",
                "content": [],
                "meta": {"provider": "p", "model": "m", "cost": {"input": 0.01, "total": 0.02}},
            },
            {
                "role": "compact",
                "content": [],
                "meta": {"provider": "p", "model": "m", "cost": {"total": 0.005}},
            },
        ]
        for record in records:
            await store.append_message("s1", record)
        await store.append_rewind("s1", 0)

        assert await load_session_cost(store, "s1") == pytest.approx(0.025)

    @pytest.mark.asyncio
    async def test_skips_records_without_cost(self, tmp_path: Path) -> None:
        store = SessionStore(data_dir=tmp_path)
        await store.create_session("s1", cwd="/tmp")
        await store.append_message(
            "s1",
            {
                "role": "assistant",
                "content": [],
                "meta": {"provider": "p", "model": "m", "cost": {"total": 0.02}},
            },
        )
        # A cancelled stream without cost must not hide known session costs.
        await store.append_message("s1", {"role": "assistant", "content": [], "meta": {"provider": "p", "model": "m"}})

        assert await load_session_cost(store, "s1") == pytest.approx(0.02)

        await store.create_session("s2", cwd="/tmp")
        await store.append_message(
            "s2",
            {
                "role": "assistant",
                "content": [],
                "meta": {"provider": "unknown", "model": "unknown", "usage": {"input_tokens": 10, "output_tokens": 5}},
            },
        )
        assert await load_session_cost(store, "s2") is None


def test_cli_rejects_non_positive_max_turns() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["run", "--max-turns", "0", "hello"])

    assert result.exit_code != 0


def test_web_dev_enables_backend_reload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import uvicorn
    from typer.testing import CliRunner

    import mycode_cli.main as main_module

    run_args: dict[str, Any] = {}

    def fake_run(app_ref: Any, **kwargs: Any) -> None:
        run_args.update({"app_ref": app_ref, **kwargs})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "get_settings", lambda cwd: settings_for(cwd))
    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = CliRunner().invoke(app, ["web", "--dev", "--port", "8765"])

    assert result.exit_code == 0, result.output
    assert run_args["app_ref"] == "mycode_cli.server.app:create_api_app"
    assert run_args["reload"] is True
    assert run_args["factory"] is True


@pytest.mark.asyncio
class TestPromptInput:
    async def test_enter_submits_unique_slash_completion(self) -> None:
        with create_pipe_input() as pipe_input:
            session = PromptSession(
                history=InMemoryHistory(),
                completer=_PromptCompleter(),
                key_bindings=_build_chat_key_bindings(),
                multiline=True,
                prompt_continuation="  ",
                input=pipe_input,
                output=DummyOutput(),
            )

            async def drive_input() -> None:
                await asyncio.sleep(0.05)
                pipe_input.send_text("/p")
                await asyncio.sleep(0.1)
                pipe_input.send_text("\t")
                await asyncio.sleep(0.1)
                pipe_input.send_text("\r")

            task = asyncio.create_task(drive_input())
            try:
                result = await session.prompt_async("> ")
            finally:
                await task

        assert result == "/provider"

    async def test_enter_accepts_ambiguous_slash_completion_before_submit(self) -> None:
        with create_pipe_input() as pipe_input:
            session = PromptSession(
                history=InMemoryHistory(),
                completer=_PromptCompleter(),
                key_bindings=_build_chat_key_bindings(),
                multiline=True,
                prompt_continuation="  ",
                input=pipe_input,
                output=DummyOutput(),
            )

            async def drive_input() -> None:
                await asyncio.sleep(0.05)
                pipe_input.send_text("/r")
                await asyncio.sleep(0.1)
                pipe_input.send_text("\t")
                await asyncio.sleep(0.1)
                pipe_input.send_text("\r")
                await asyncio.sleep(0.1)
                pipe_input.send_text("\r")

            task = asyncio.create_task(drive_input())
            try:
                result = await session.prompt_async("> ")
            finally:
                await task

        assert result == "/resume"

    async def test_enter_accepts_path_completion(self, tmp_path: Path) -> None:
        (tmp_path / "folder").mkdir()
        (tmp_path / "folder" / "bar.txt").write_text("x", encoding="utf-8")

        with create_pipe_input() as pipe_input:
            session = PromptSession(
                history=InMemoryHistory(),
                completer=_PromptCompleter(cwd=str(tmp_path)),
                key_bindings=_build_chat_key_bindings(),
                multiline=True,
                prompt_continuation="  ",
                input=pipe_input,
                output=DummyOutput(),
            )

            async def drive_input() -> None:
                await asyncio.sleep(0.05)
                pipe_input.send_text("@f")
                await asyncio.sleep(0.1)
                pipe_input.send_text("\t")
                await asyncio.sleep(0.1)
                pipe_input.send_text("\r")
                await asyncio.sleep(0.1)
                pipe_input.send_text("\r")
                await asyncio.sleep(0.1)
                pipe_input.send_text("\r")

            task = asyncio.create_task(drive_input())
            try:
                result = await session.prompt_async("> ")
            finally:
                await task

        assert result == "@folder/bar.txt"

    async def test_bracketed_paste_rewrites_existing_paths(self, tmp_path: Path) -> None:
        image_a = tmp_path / "a.png"
        image_b = tmp_path / "b b.jpg"
        note = tmp_path / "note.txt"
        image_a.write_bytes(b"x")
        image_b.write_bytes(b"x")
        note.write_text("x", encoding="utf-8")

        async def prompt_with_paste(pasted: str) -> str:
            with create_pipe_input() as pipe_input:
                session = PromptSession(
                    history=InMemoryHistory(),
                    key_bindings=_build_chat_key_bindings(),
                    multiline=True,
                    prompt_continuation="  ",
                    input=pipe_input,
                    output=DummyOutput(),
                )

                async def drive_input() -> None:
                    await asyncio.sleep(0.05)
                    pipe_input.send_bytes(b"\x1b[200~" + pasted.encode() + b"\x1b[201~")
                    await asyncio.sleep(0.05)
                    pipe_input.send_text("\r")

                task = asyncio.create_task(drive_input())
                try:
                    return await session.prompt_async("> ")
                finally:
                    await task

        assert await prompt_with_paste(str(image_a)) == f"@{image_a}"
        assert await prompt_with_paste(f'"{image_b}"') == f"@'{image_b}'"
        assert await prompt_with_paste(f"{image_a} '{image_b}'") == f"@{image_a} @'{image_b}'"
        assert await prompt_with_paste(str(note)) == f"@{note}"

    async def test_bracketed_paste_keeps_non_file_text_unchanged(self) -> None:
        with create_pipe_input() as pipe_input:
            session = PromptSession(
                history=InMemoryHistory(),
                key_bindings=_build_chat_key_bindings(),
                multiline=True,
                prompt_continuation="  ",
                input=pipe_input,
                output=DummyOutput(),
            )

            async def drive_input() -> None:
                await asyncio.sleep(0.05)
                pipe_input.send_bytes(b"\x1b[200~hello world\x1b[201~")
                await asyncio.sleep(0.05)
                pipe_input.send_text("\r")

            task = asyncio.create_task(drive_input())
            try:
                result = await session.prompt_async("> ")
            finally:
                await task

        assert result == "hello world"


class TestAttachments:
    def test_builds_message_with_text_and_image_attachments(self, tmp_path: Path, cli_home: Path) -> None:
        code_file = tmp_path / "main.py"
        image_file = tmp_path / "diagram.png"
        code_file.write_text("print('hello')\n", encoding="utf-8")
        image_file.write_bytes(b"\x89PNG\r\n\x1a\nrest")

        chat = TerminalChat(
            agent=cast(Any, _AttachmentAgent(cwd=str(tmp_path))),
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
            agent=cast(Any, _AttachmentAgent(cwd=str(tmp_path))),
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
                _AttachmentAgent(cwd=str(tmp_path), **agent_kwargs),
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
                _AttachmentAgent(cwd=str(tmp_path), supports_pdf_input=False),
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
            agent=cast(Any, _AttachmentAgent(cwd=str(tmp_path))),
            settings=settings_for(str(tmp_path)),
            store=cast(Any, object()),
            session_id="test-session",
        )
        message = chat._build_user_message(f"check @{binary_file}")

        assert len(message["content"]) == 1
        assert message["content"][0]["type"] == "text"
