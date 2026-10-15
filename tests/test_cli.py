"""Tests for the CLI entrypoint: non-interactive runs, session resolution, and commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from mycode import ErrorEvent, ReasoningEvent, TextEvent, ToolDoneEvent
from mycode_cli.main import app, resolve_session_id, run_noninteractive
from mycode_cli.permissions import PERMISSION_DENIED_BY_USER_OUTPUT, PERMISSION_DENIED_OUTPUT
from mycode_cli.sessions import SessionStore


class _FakeAgent:
    session_id = "session"

    async def achat(self, message: str, *, on_persist=None):
        if on_persist:
            await on_persist({"role": "user", "content": [{"type": "text", "text": message}]})
            await on_persist(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Persisted final answer"}],
                }
            )
        yield ReasoningEvent(delta="Hidden reasoning")
        yield TextEvent(delta="Streamed answer should stay hidden")


class _ErrorAgent:
    session_id = "session"

    async def achat(self, message: str, *, on_persist=None):
        yield ErrorEvent(message="provider error")


class _PermissionDeniedAgent:
    session_id = "session"

    def __init__(self, output: str) -> None:
        self.output = output

    async def achat(self, message: str, *, on_persist=None):
        yield ToolDoneEvent(tool_use_id="call-1", output=self.output, is_error=True)


class _PermissionDeniedThenReplyAgent:
    session_id = "session"

    async def achat(self, message: str, *, on_persist=None):
        yield ToolDoneEvent(tool_use_id="call-1", output=PERMISSION_DENIED_OUTPUT, is_error=True)
        if on_persist:
            await on_persist(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Permission was denied. Use --permission standard."}],
                }
            )


class _FakeSetup:
    store = None
    settings = type("Settings", (), {"cwd": "."})()
    resolved_provider = None
    session_id = "session"


class TestRunNoninteractive:
    @pytest.mark.asyncio
    async def test_prints_only_final_reply(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = await run_noninteractive(
            cast(Any, _FakeAgent()), "hello", store=SessionStore(tmp_path), cwd=str(tmp_path)
        )

        captured = capsys.readouterr()
        assert code == 0
        assert captured.out == "Persisted final answer\n"
        assert captured.err == ""

    @pytest.mark.asyncio
    async def test_prints_errors_to_stderr(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = await run_noninteractive(
            cast(Any, _ErrorAgent()), "hello", store=SessionStore(tmp_path), cwd=str(tmp_path)
        )

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert captured.err == "provider error\n"

    @pytest.mark.parametrize("output", [PERMISSION_DENIED_OUTPUT, PERMISSION_DENIED_BY_USER_OUTPUT])
    async def test_prints_permission_denials_to_stderr(
        self, output: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await run_noninteractive(
            cast(Any, _PermissionDeniedAgent(output)), "hello", store=SessionStore(tmp_path), cwd=str(tmp_path)
        )

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert captured.err == f"{output}\n"

    @pytest.mark.asyncio
    async def test_prints_final_reply_after_permission_denial(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await run_noninteractive(
            cast(Any, _PermissionDeniedThenReplyAgent()),
            "hello",
            store=SessionStore(tmp_path),
            cwd=str(tmp_path),
        )

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == "Permission was denied. Use --permission standard.\n"
        assert captured.err == ""


@pytest.mark.asyncio
class TestResolveSessionId:
    async def test_defaults_to_new_session(self, tmp_path: Path) -> None:
        store = SessionStore(data_dir=tmp_path / "sessions")

        session_id = await resolve_session_id(
            store=store,
            cwd=str(tmp_path),
            requested_session_id=None,
            continue_last=False,
        )

        assert session_id
        assert await store.load_metadata(session_id) is None
        assert await store.list_sessions() == []

    async def test_continue_reuses_latest_session(self, tmp_path: Path) -> None:
        store = SessionStore(data_dir=tmp_path / "sessions")
        await store.create_session("first", cwd=str(tmp_path))
        await store.create_session("second", cwd=str(tmp_path))

        session_id = await resolve_session_id(
            store=store,
            cwd=str(tmp_path),
            requested_session_id=None,
            continue_last=True,
        )

        assert session_id == "second"

    async def test_explicit_missing_session_errors(self, tmp_path: Path) -> None:
        store = SessionStore(data_dir=tmp_path / "sessions")

        with pytest.raises(ValueError, match="Unknown session"):
            await resolve_session_id(
                store=store,
                cwd=str(tmp_path),
                requested_session_id="missing",
                continue_last=False,
            )


def test_cli_rejects_non_positive_max_turns() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["run", "--max-turns", "0", "hello"])

    assert result.exit_code != 0


def test_web_dev_enables_backend_reload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import uvicorn
    from typer.testing import CliRunner

    run_args: dict[str, Any] = {}

    def fake_run(app_ref: Any, **kwargs: Any) -> None:
        run_args.update({"app_ref": app_ref, **kwargs})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = CliRunner().invoke(app, ["web", "--dev", "--port", "8765"])

    assert result.exit_code == 0, result.output
    assert run_args["app_ref"] == "mycode_cli.server.app:create_api_app"
    assert run_args["reload"] is True
    assert run_args["factory"] is True


def test_cli_rejects_session_options_before_subcommand() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["--model", "foo", "run", "hello"])

    assert result.exit_code == 2
    assert "only apply to the interactive session" in result.output


@pytest.mark.parametrize(
    ("args", "stdin", "expected"),
    [
        (["review", "this"], "", "review this"),
        ([], "piped prompt\n", "piped prompt"),
        (["review", "this"], "diff --git a b\n", "review this\n\ndiff --git a b"),
    ],
)
def test_run_reads_prompt_from_args_and_stdin(
    monkeypatch: pytest.MonkeyPatch, args: list[str], stdin: str, expected: str
) -> None:
    from typer.testing import CliRunner

    import mycode_cli.main as main

    sent: list[str] = []

    async def fake_run(agent: Any, message: str, **kwargs: Any) -> int:
        sent.append(message)
        return 0

    monkeypatch.setattr(main, "build_agent", lambda **kwargs: None)
    monkeypatch.setattr(main, "run_noninteractive", fake_run)
    monkeypatch.setattr(main, "_bootstrap", lambda **kwargs: _FakeSetup())

    result = CliRunner().invoke(app, ["run", *args], input=stdin)

    assert result.exit_code == 0, result.output
    assert sent == [expected]


def test_run_requires_a_prompt() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["run"], input="")

    assert result.exit_code == 2
    assert "provide a prompt" in result.output
