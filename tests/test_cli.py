"""Tests for CLI runtime and terminal behavior."""

import asyncio
from typing import Any, cast

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from mycode.cli.chat import _build_chat_key_bindings, _SlashCompleter, history_file_path
from mycode.cli.main import app, run_noninteractive
from mycode.cli.render import TerminalView
from mycode.cli.runtime import list_model_options, resolve_session
from mycode.cli.runtime import update_agent_runtime as _update_agent_runtime
from mycode.core.agent import Event
from mycode.core.config import ProviderConfig, ResolvedProvider, Settings
from mycode.core.session import SessionStore


class _FakeStore:
    async def append_message(self, session_id: str, payload: dict, **_: Any) -> None:
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


@pytest.mark.asyncio
async def test_run_noninteractive_prints_only_final_reply(capsys):
    code = await run_noninteractive(
        cast(Any, _FakeAgent()),
        store=cast(Any, _FakeStore()),
        session_id="test-session",
        message="hello",
    )

    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == "Persisted final answer\n"
    assert captured.err == ""


class _ErrorAgent:
    async def achat(self, message: str, *, on_persist=None):
        yield Event("error", {"message": "provider error"})


@pytest.mark.asyncio
async def test_run_noninteractive_prints_errors_to_stderr(capsys):
    code = await run_noninteractive(
        cast(Any, _ErrorAgent()),
        store=cast(Any, _FakeStore()),
        session_id="test-session",
        message="hello",
    )

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "provider error\n"


@pytest.mark.asyncio
async def test_resolve_session_defaults_to_new(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")

    resolved = await resolve_session(
        store=store,
        provider="anthropic",
        cwd=str(tmp_path),
        model="gpt-5.4",
        api_base=None,
        requested_session_id=None,
        continue_last=False,
    )

    assert resolved.mode == "new"
    assert resolved.messages == []
    assert resolved.session["cwd"] == str(tmp_path)
    assert await store.list_sessions() == []


@pytest.mark.asyncio
async def test_resolve_session_continue_reuses_latest(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")
    first = await store.create_session("First", model="gpt-5.4", cwd=str(tmp_path), api_base=None)
    second = await store.create_session("Second", model="gpt-5.4", cwd=str(tmp_path), api_base=None)
    await store.append_message(
        second["session"]["id"],
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        provider="anthropic",
        model="gpt-5.4",
        cwd=str(tmp_path),
        api_base=None,
    )

    resolved = await resolve_session(
        store=store,
        provider="anthropic",
        cwd=str(tmp_path),
        model="gpt-5.4",
        api_base=None,
        requested_session_id=None,
        continue_last=True,
    )

    assert resolved.mode == "resumed"
    assert resolved.session_id != first["session"]["id"]
    assert resolved.session_id == second["session"]["id"]
    assert resolved.messages[0]["content"] == [{"type": "text", "text": "hello"}]


@pytest.mark.asyncio
async def test_resolve_session_explicit_missing_id_errors(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")

    with pytest.raises(ValueError, match="Unknown session"):
        await resolve_session(
            store=store,
            provider="anthropic",
            cwd=str(tmp_path),
            model="gpt-5.4",
            api_base=None,
            requested_session_id="missing",
            continue_last=False,
        )


def test_cli_rejects_non_positive_max_turns():
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--max-turns", "0", "hello"])

    assert result.exit_code != 0


def test_cli_shows_help_for_web():
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(app, ["web", "--help"])

    assert result.exit_code == 0
    assert "--dev" in result.output


def test_history_file_path_uses_mycode_home(tmp_path, monkeypatch):
    mycode_home = tmp_path / ".mycode"
    monkeypatch.setenv("MYCODE_HOME", str(mycode_home))

    path = history_file_path()

    assert path == str((mycode_home / "cli_history").resolve())
    assert mycode_home.exists()


def test_history_preview_entries_summarize_tool_only_assistant_messages():
    view = TerminalView()

    entries = view.history_preview_entries(
        [
            {"role": "user", "content": [{"type": "text", "text": "Inspect project"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "a", "name": "read", "input": {}},
                    {"type": "tool_use", "id": "b", "name": "bash", "input": {}},
                ],
            },
        ]
    )

    assert entries == [
        ("You", "Inspect project"),
        ("Assistant", "[Used tools: read, bash]"),
    ]


@pytest.mark.asyncio
async def test_chat_prompt_enter_submits_selected_slash_completion():
    with create_pipe_input() as pipe_input:
        session = PromptSession(
            history=InMemoryHistory(),
            completer=_SlashCompleter(),
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


def test_model_options_use_configured_provider_models():
    settings = Settings(
        providers={
            "claude": ProviderConfig(
                name="claude",
                type="anthropic",
                models=["claude-sonnet-4-6", "claude-haiku-4-5"],
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
    def __init__(self, *, cwd: str, settings: Settings) -> None:
        self.cwd = cwd
        self.provider = "anthropic"
        self.model = "claude-sonnet-4-6"
        self.api_key = None
        self.api_base = None
        self.reasoning_effort = None
        self.max_tokens = 8192
        self.settings = settings


@pytest.mark.asyncio
async def test_update_agent_runtime_updates_agent_without_rewriting_session(tmp_path, monkeypatch):
    store = SessionStore(data_dir=tmp_path / "sessions")
    created = await store.create_session(
        None,
        provider="anthropic",
        model="claude-sonnet-4-6",
        cwd=str(tmp_path),
        api_base=None,
    )
    session_id = created["session"]["id"]
    await store.append_message(
        session_id,
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        provider="anthropic",
        model="claude-sonnet-4-6",
        cwd=str(tmp_path),
        api_base=None,
    )

    old_settings = Settings(
        providers={},
        default_provider=None,
        default_model=None,
        port=8000,
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        config_paths=[],
    )
    new_settings = Settings(
        providers={},
        default_provider=None,
        default_model=None,
        port=8000,
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        config_paths=[],
    )
    agent = _RuntimeAgent(cwd=str(tmp_path), settings=old_settings)

    monkeypatch.setattr("mycode.cli.runtime.get_settings", lambda cwd: new_settings)
    monkeypatch.setattr(
        "mycode.cli.runtime.resolve_provider",
        lambda settings, provider_name=None, model=None: ResolvedProvider(
            provider="openai",
            model="gpt-5.4",
            api_key="test-key",
            api_base="https://api.openai.com/v1",
            reasoning_effort="medium",
            max_tokens=16000,
        ),
    )

    changed = await _update_agent_runtime(
        cast(Any, agent),
        provider_name="openai",
        model=None,
    )

    assert changed is True
    assert agent.provider == "openai"
    assert agent.model == "gpt-5.4"
    assert agent.api_key == "test-key"
    assert agent.api_base == "https://api.openai.com/v1"
    assert agent.reasoning_effort == "medium"
    assert agent.max_tokens == 16000
    assert agent.settings is new_settings

    loaded = await store.load_session(session_id)
    assert loaded is not None
    assert loaded["session"]["provider"] == "anthropic"
    assert loaded["session"]["model"] == "claude-sonnet-4-6"
    assert loaded["session"]["api_base"] is None


@pytest.mark.asyncio
async def test_list_cli_sessions_filters_current_workspace(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")
    current_cwd = str(tmp_path / "project-a")
    other_cwd = str(tmp_path / "project-b")

    current_session = await store.create_session(
        "Current",
        model="gpt-5.4",
        cwd=current_cwd,
        api_base=None,
    )
    other_session = await store.create_session(
        "Other",
        model="gpt-5.4",
        cwd=other_cwd,
        api_base=None,
    )
    await store.append_message(
        current_session["session"]["id"],
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        provider="anthropic",
        model="gpt-5.4",
        cwd=current_cwd,
        api_base=None,
    )
    await store.append_message(
        other_session["session"]["id"],
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        provider="anthropic",
        model="gpt-5.4",
        cwd=other_cwd,
        api_base=None,
    )

    current = await store.list_sessions(cwd=current_cwd)
    all_sessions = await store.list_sessions(cwd=None)

    assert len(current) == 1
    assert current[0]["title"] == "Current"
    assert len(all_sessions) == 2
