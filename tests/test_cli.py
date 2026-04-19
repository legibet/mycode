"""Tests for CLI runtime and terminal behavior."""

import asyncio
import base64
import html
import shlex
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from mycode.agent import Event
from mycode.messages import ConversationMessage
from mycode.session import SessionStore
from mycode.tools import DEFAULT_TOOL_SPECS, ToolContext, ToolExecutor
from mycode_cli.config import ModelConfig, ProviderConfig, ResolvedProvider, Settings
from mycode_cli.main import app, run_noninteractive
from mycode_cli.runtime import apply_resolved_provider, list_model_options, resolve_session
from mycode_cli.tui.chat import TerminalChat, _build_chat_key_bindings, _PromptCompleter, _rewrite_pasted_file_paths
from mycode_cli.tui.render import TerminalView


class _FakeStore:
    async def append_message(self, session_id: str, payload: ConversationMessage, **_: Any) -> None:
        return None


class _AttachmentAgent:
    def __init__(
        self,
        *,
        cwd: str,
        session_dir: Path,
        supports_image_input: bool = True,
        supports_pdf_input: bool = True,
    ) -> None:
        self.cwd = cwd
        self.supports_image_input = supports_image_input
        self.supports_pdf_input = supports_pdf_input
        self.tools = ToolExecutor(DEFAULT_TOOL_SPECS)
        self.tool_ctx = ToolContext(
            executor=self.tools,
            cwd=cwd,
            tool_output_dir=session_dir,
            supports_image_input=supports_image_input,
        )


def _settings_for(cwd: str) -> Settings:
    return Settings(
        providers={},
        default_provider=None,
        default_model=None,
        port=8000,
        cwd=cwd,
        workspace_root=cwd,
        config_paths=[],
    )


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


@pytest.mark.asyncio
async def test_run_noninteractive_prints_only_final_reply(capsys):
    code = await run_noninteractive(cast(Any, _FakeAgent()), "hello")

    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == "Persisted final answer\n"
    assert captured.err == ""


class _ErrorAgent:
    async def achat(self, message: str, *, on_persist=None):
        yield Event("error", {"message": "provider error"})


@pytest.mark.asyncio
async def test_run_noninteractive_prints_errors_to_stderr(capsys):
    code = await run_noninteractive(cast(Any, _ErrorAgent()), "hello")

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "provider error\n"


@pytest.mark.asyncio
async def test_resolve_session_defaults_to_new(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")

    resolved = await resolve_session(
        store=store,
        cwd=str(tmp_path),
        requested_session_id=None,
        continue_last=False,
    )

    assert resolved.mode == "new"
    assert resolved.messages == []
    # New sessions are allocated an id but not yet written to disk.
    assert resolved.session_id
    assert await store.list_sessions() == []


@pytest.mark.asyncio
async def test_resolve_session_continue_reuses_latest(tmp_path):
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


@pytest.mark.asyncio
async def test_resolve_session_explicit_missing_id_errors(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")

    with pytest.raises(ValueError, match="Unknown session"):
        await resolve_session(
            store=store,
            cwd=str(tmp_path),
            requested_session_id="missing",
            continue_last=False,
        )


def test_history_preview_shows_last_three_turns_with_assistant_text_and_tools():
    view = TerminalView(Console(file=StringIO(), force_terminal=False, color_system=None, width=120))

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "turn one"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "answer one"}]},
        {"role": "user", "content": [{"type": "text", "text": "turn two"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "text": "hidden"},
                {"type": "text", "text": "answer two"},
                {"type": "tool_use", "name": "read", "input": {"path": "src/two.py"}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ignored"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done two"}]},
        {"role": "user", "content": [{"type": "text", "text": "turn three"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "bash", "input": {"command": "pytest tests/test_cli.py -q"}},
                {"type": "text", "text": "done three"},
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "turn four"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking four\nmore detail"},
                {"type": "tool_use", "name": "edit", "input": {"path": "src/four.py"}},
                {"type": "text", "text": "done four"},
            ],
        },
    ]

    assert view.history_preview_entries(messages) == [
        [
            ("user", "turn two"),
            ("text", "answer two"),
            ("tool", ("read", {"path": "src/two.py"})),
            ("text", "done two"),
        ],
        [
            ("user", "turn three"),
            ("tool", ("bash", {"command": "pytest tests/test_cli.py -q"})),
            ("text", "done three"),
        ],
        [
            ("user", "turn four"),
            ("text", "checking four\nmore detail"),
            ("tool", ("edit", {"path": "src/four.py"})),
            ("text", "done four"),
        ],
    ]


def test_history_preview_keeps_latest_user_turn_without_assistant_reply():
    view = TerminalView(Console(file=StringIO(), force_terminal=False, color_system=None, width=120))

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
        {"role": "user", "content": [{"type": "text", "text": "latest question"}]},
    ]

    assert view.history_preview_entries(messages) == [
        [("user", "first"), ("text", "reply")],
        [("user", "latest question")],
    ]


def test_history_preview_skips_attached_file_payload_blocks():
    view = TerminalView(Console(file=StringIO(), force_terminal=False, color_system=None, width=120))

    messages = [
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
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]

    assert view.history_preview_entries(messages) == [[("user", "check @main.py"), ("text", "done")]]


def test_print_history_preview_renders_transcript_style():
    output = StringIO()
    view = TerminalView(Console(file=output, force_terminal=False, color_system=None, width=120))

    view.print_history_preview(
        [
            {"role": "user", "content": [{"type": "text", "text": "question\n\n- item"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "checking `foo`"},
                    {"type": "tool_use", "name": "read", "input": {"path": "foo.py"}},
                    {"type": "text", "text": "```py\nprint(1)\n```"},
                ],
            },
        ]
    )

    rendered = output.getvalue()
    assert rendered.startswith("recent\n\n❯ question\n  \n  - item\nchecking foo")
    assert "\n⏺ Read  foo.py\n" in rendered
    assert rendered.endswith("print(1)\n")


def test_cli_rejects_non_positive_max_turns():
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--max-turns", "0", "hello"])

    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_chat_prompt_enter_submits_selected_slash_completion():
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


def test_prompt_completer_suggests_paths_for_at_references(tmp_path):
    (tmp_path / "app.py").write_text("print('x')\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "a b.py").write_text("print('y')\n", encoding="utf-8")

    completer = _PromptCompleter(cwd=str(tmp_path))
    completions = list(completer.get_completions(Document("@"), CompleteEvent()))

    assert any(item.text == "@app.py" for item in completions)
    assert any(item.text == "@src/" for item in completions)
    assert any(item.text == "@'a b.py'" for item in completions)


def test_prompt_completer_keeps_quotes_for_paths_with_spaces(tmp_path):
    (tmp_path / "a b.py").write_text("print('y')\n", encoding="utf-8")

    completer = _PromptCompleter(cwd=str(tmp_path))
    completions = list(completer.get_completions(Document('@"a'), CompleteEvent()))

    assert any(item.text == '@"a b.py"' for item in completions)


def test_rewrite_pasted_file_paths_rewrites_only_existing_files(tmp_path):
    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b b.jpg"
    note = tmp_path / "note.txt"
    image_a.write_bytes(b"x")
    image_b.write_bytes(b"x")
    note.write_text("x")

    assert _rewrite_pasted_file_paths(str(image_a)) == f"@{image_a}"
    assert _rewrite_pasted_file_paths(f'"{image_b}"') == f"@'{image_b}'"
    assert _rewrite_pasted_file_paths(f"{image_a} '{image_b}'") == f"@{image_a} @'{image_b}'"
    assert _rewrite_pasted_file_paths(str(note)) == f"@{note}"
    assert _rewrite_pasted_file_paths("hello world") is None


def test_terminal_chat_builds_user_message_with_text_and_image_attachments(tmp_path):
    code_file = tmp_path / "main.py"
    image_file = tmp_path / "diagram.png"
    code_file.write_text("print('hello')\n", encoding="utf-8")
    image_file.write_bytes(b"\x89PNG\r\n\x1a\nrest")

    chat = TerminalChat(
        agent=cast(Any, _AttachmentAgent(cwd=str(tmp_path), session_dir=tmp_path / ".session")),
        settings=_settings_for(str(tmp_path)),
        store=cast(Any, _FakeStore()),
        session_id="test-session",
    )
    message = chat._build_user_message(f"check @{code_file} @{image_file}")

    assert message["role"] == "user"
    assert message["content"][0] == {"type": "text", "text": f"check @{code_file} @{image_file}"}
    assert message["content"][1]["type"] == "text"
    assert message["content"][1]["meta"] == {"attachment": True, "path": str(code_file)}
    assert message["content"][1]["text"].startswith(f'<file name="{code_file}">\n')
    assert "print('hello')" in message["content"][1]["text"]
    assert message["content"][2] == {
        "type": "image",
        "data": base64.b64encode(image_file.read_bytes()).decode("utf-8"),
        "mime_type": "image/png",
        "name": "diagram.png",
    }


def test_terminal_chat_builds_text_notice_for_image_attachment_without_image_input(tmp_path):
    image_file = tmp_path / "diagram.png"
    image_file.write_bytes(b"\x89PNG\r\n\x1a\nrest")

    chat = TerminalChat(
        agent=cast(
            Any,
            _AttachmentAgent(
                cwd=str(tmp_path),
                session_dir=tmp_path / ".session",
                supports_image_input=False,
            ),
        ),
        settings=_settings_for(str(tmp_path)),
        store=cast(Any, _FakeStore()),
        session_id="test-session",
    )
    message = chat._build_user_message(f"check @{image_file}")

    assert message["role"] == "user"
    assert message["content"][0] == {"type": "text", "text": f"check @{image_file}"}
    assert message["content"][1] == {
        "type": "text",
        "text": f'<file name="{image_file}" media_type="image/png" kind="image">Current model does not support image input.</file>',
        "meta": {"attachment": True, "path": str(image_file)},
    }


def test_terminal_chat_builds_user_message_with_pdf_attachment(tmp_path):
    pdf_file = tmp_path / "report.pdf"
    pdf_file.write_bytes(b"%PDF-1.7\nrest")

    chat = TerminalChat(
        agent=cast(Any, _AttachmentAgent(cwd=str(tmp_path), session_dir=tmp_path / ".session")),
        settings=_settings_for(str(tmp_path)),
        store=cast(Any, _FakeStore()),
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


def test_terminal_chat_builds_text_notice_for_pdf_attachment_without_pdf_input(tmp_path):
    pdf_file = tmp_path / 'report <"draft">.pdf'
    pdf_file.write_bytes(b"%PDF-1.7\nrest")

    chat = TerminalChat(
        agent=cast(
            Any,
            _AttachmentAgent(
                cwd=str(tmp_path),
                session_dir=tmp_path / ".session",
                supports_pdf_input=False,
            ),
        ),
        settings=_settings_for(str(tmp_path)),
        store=cast(Any, _FakeStore()),
        session_id="test-session",
    )
    message = chat._build_user_message(f"check @{shlex.quote(str(pdf_file))}")

    assert message["role"] == "user"
    assert message["content"][0] == {
        "type": "text",
        "text": f"check @{shlex.quote(str(pdf_file))}",
    }
    assert message["content"][1] == {
        "type": "text",
        "text": f'<file name="{html.escape(str(pdf_file), quote=True)}" media_type="application/pdf" kind="document">Current model does not support PDF input.</file>',
        "meta": {"attachment": True, "path": str(pdf_file)},
    }


@pytest.mark.asyncio
async def test_chat_prompt_bracketed_paste_rewrites_file_paths(tmp_path):
    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b b.jpg"
    note = tmp_path / "note.txt"
    image_a.write_bytes(b"x")
    image_b.write_bytes(b"x")
    note.write_text("x")

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


def test_model_options_use_configured_provider_models():
    settings = Settings(
        providers={
            "claude": ProviderConfig(
                name="claude",
                type="anthropic",
                models={
                    "claude-sonnet-4-6": ModelConfig(),
                    "claude-haiku-4-5": ModelConfig(),
                },
                base_url="https://api.anthropic.com",
            )
        },
        default_provider=None,
        default_model=None,
        port=8000,
        cwd="/tmp/project",
        workspace_root="/tmp/project",
        config_paths=[],
    )

    assert list_model_options(
        settings,
        provider="anthropic",
        api_base="https://api.anthropic.com",
        current_model="claude-haiku-4-5",
    ) == ["claude-haiku-4-5", "claude-sonnet-4-6"]


class _RuntimeAgent:
    def __init__(self, *, cwd: str) -> None:
        self.cwd = cwd
        self.provider = "anthropic"
        self.model = "claude-sonnet-4-6"
        self.api_key = None
        self.api_base = None
        self.reasoning_effort = None
        self.max_tokens = 16_384
        self.context_window = 128_000
        self.supports_reasoning: bool | None = None
        self.supports_image_input = False
        self.supports_pdf_input = False
        self.tools = ToolExecutor(DEFAULT_TOOL_SPECS)

    def refresh_capabilities(self, **_: Any) -> None:
        """No-op capability refresh — the real inference lives on ``Agent``."""


@pytest.mark.asyncio
async def test_apply_resolved_provider_updates_agent_without_rewriting_session(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")
    await store.create_session("s1", cwd=str(tmp_path))
    await store.append_message(
        "s1",
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    )

    agent = _RuntimeAgent(cwd=str(tmp_path))
    resolved = ResolvedProvider(
        provider="openai",
        model="gpt-5.4",
        api_key="test-key",
        api_base="https://api.openai.com/v1",
        reasoning_effort="medium",
    )

    changed = apply_resolved_provider(cast(Any, agent), resolved, _settings_for(str(tmp_path)))

    assert changed is True
    assert agent.provider == "openai"
    assert agent.model == "gpt-5.4"
    assert agent.api_key == "test-key"
    assert agent.api_base == "https://api.openai.com/v1"
    assert agent.reasoning_effort == "medium"


@pytest.mark.asyncio
async def test_list_cli_sessions_filters_current_workspace(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")
    current_cwd = str(tmp_path / "project-a")
    other_cwd = str(tmp_path / "project-b")

    await store.create_session("current", cwd=current_cwd)
    await store.create_session("other", cwd=other_cwd)
    await store.append_message(
        "current",
        {"role": "user", "content": [{"type": "text", "text": "hello current"}]},
    )
    await store.append_message(
        "other",
        {"role": "user", "content": [{"type": "text", "text": "hello other"}]},
    )

    current = await store.list_sessions(cwd=current_cwd)
    all_sessions = await store.list_sessions(cwd=None)

    assert len(current) == 1
    assert current[0]["id"] == "current"
    assert len(all_sessions) == 2
