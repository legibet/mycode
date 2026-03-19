"""Tests for CLI output behavior."""

from typing import Any, cast

import pytest

from mycode.cli import (
    _build_parser,
    _history_file_path,
    _history_preview_entries,
    _model_options,
    _positive_int,
    _resolve_session_choice,
    _select_list_item,
    _switch_agent_runtime,
    resolve_cli_session,
    run_once,
)
from mycode.core.agent import Event
from mycode.core.config import ProviderConfig, ResolvedProvider, Settings
from mycode.core.session import SessionStore


class _FakeConsole:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def print(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


class _FakeStore:
    async def append_message(self, session_id: str, payload: dict) -> None:
        return None


class _FakeAgent:
    async def achat(self, message: str, *, on_persist=None):
        yield Event("reasoning", {"content": "Hidden reasoning"})
        yield Event("text", {"content": "Visible answer"})


async def test_run_once_prints_reasoning_output(monkeypatch):
    fake_console = _FakeConsole()
    monkeypatch.setattr("mycode.cli.console", fake_console)

    code = await run_once(
        cast(Any, _FakeAgent()),
        store=cast(Any, _FakeStore()),
        session_id="test-session",
        message="hello",
    )

    assert code == 0
    printed = [str(args[0]) for args, _kwargs in fake_console.calls if args]
    assert "Hidden reasoning" in printed
    assert "Visible answer" in printed


@pytest.mark.asyncio
async def test_resolve_cli_session_defaults_to_new(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")

    resolved = await resolve_cli_session(
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


@pytest.mark.asyncio
async def test_resolve_cli_session_continue_reuses_latest(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")
    first = await store.create_session("First", model="gpt-5.4", cwd=str(tmp_path), api_base=None)
    second = await store.create_session("Second", model="gpt-5.4", cwd=str(tmp_path), api_base=None)
    await store.append_message(
        second["session"]["id"], {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    )

    resolved = await resolve_cli_session(
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
async def test_resolve_cli_session_explicit_missing_id_errors(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")

    with pytest.raises(ValueError, match="Unknown session"):
        await resolve_cli_session(
            store=store,
            provider="anthropic",
            cwd=str(tmp_path),
            model="gpt-5.4",
            api_base=None,
            requested_session_id="missing",
            continue_last=False,
        )


def test_positive_int_accepts_positive_values():
    assert _positive_int("3") == 3


def test_positive_int_rejects_non_positive_values():
    with pytest.raises(Exception, match="positive integer"):
        _positive_int("0")

    with pytest.raises(Exception, match="positive integer"):
        _positive_int("-2")


def test_build_parser_accepts_max_turns_flag():
    parser = _build_parser()

    args = parser.parse_args(["run", "--max-turns", "7", "hello"])

    assert args.command == "run"
    assert args.max_turns == 7
    assert args.message == ["hello"]


def test_history_file_path_uses_mycode_home(tmp_path, monkeypatch):
    mycode_home = tmp_path / ".mycode"
    monkeypatch.setenv("MYCODE_HOME", str(mycode_home))

    history_path = _history_file_path()

    assert history_path == str((mycode_home / "cli_history").resolve())
    assert mycode_home.exists()


def test_history_preview_entries_summarize_tool_only_assistant_messages():
    entries = _history_preview_entries(
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


def test_resolve_session_choice_accepts_index_and_id_prefix():
    sessions = [
        {"id": "abc123456789", "title": "First"},
        {"id": "def987654321", "title": "Second"},
    ]

    assert _resolve_session_choice("2", sessions) == sessions[1]
    assert _resolve_session_choice("abc123", sessions) == sessions[0]


def test_resolve_session_choice_errors_on_ambiguous_prefix():
    sessions = [
        {"id": "abc123456789", "title": "First"},
        {"id": "abc987654321", "title": "Second"},
    ]

    with pytest.raises(ValueError, match="Ambiguous session id"):
        _resolve_session_choice("abc", sessions)


def test_select_list_item_accepts_index_and_prefix():
    models = ["claude-sonnet-4-6", "claude-haiku-4-5"]

    assert _select_list_item("2", models, label="model") == "claude-haiku-4-5"
    assert _select_list_item("claude-sonnet", models, label="model") == "claude-sonnet-4-6"


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

    assert _model_options(
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
async def test_switch_agent_runtime_updates_agent_and_session(tmp_path, monkeypatch):
    store = SessionStore(data_dir=tmp_path / "sessions")
    created = await store.create_session(
        None,
        provider="anthropic",
        model="claude-sonnet-4-6",
        cwd=str(tmp_path),
        api_base=None,
    )
    session_id = created["session"]["id"]

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

    monkeypatch.setattr("mycode.cli.get_settings", lambda cwd: new_settings)
    monkeypatch.setattr(
        "mycode.cli.resolve_provider",
        lambda settings, provider_name=None, model=None: ResolvedProvider(
            provider="openai",
            model="gpt-5.4",
            api_key="test-key",
            api_base="https://api.openai.com/v1",
            reasoning_effort="medium",
            max_tokens=16000,
        ),
    )

    changed = await _switch_agent_runtime(
        cast(Any, agent),
        store=store,
        session_id=session_id,
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
    assert loaded["session"]["provider"] == "openai"
    assert loaded["session"]["model"] == "gpt-5.4"
    assert loaded["session"]["api_base"] == "https://api.openai.com/v1"


@pytest.mark.asyncio
async def test_list_cli_sessions_filters_current_workspace(tmp_path):
    store = SessionStore(data_dir=tmp_path / "sessions")
    current_cwd = str(tmp_path / "project-a")
    other_cwd = str(tmp_path / "project-b")

    await store.create_session("Current", model="gpt-5.4", cwd=current_cwd, api_base=None)
    await store.create_session("Other", model="gpt-5.4", cwd=other_cwd, api_base=None)

    current = await store.list_sessions(cwd=current_cwd)
    all_sessions = await store.list_sessions(cwd=None)

    assert len(current) == 1
    assert current[0]["title"] == "Current"
    assert len(all_sessions) == 2
