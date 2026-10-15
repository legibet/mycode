"""Tests for the CLI entrypoint: non-interactive runs, session resolution, and commands."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from mycode import ErrorEvent, ReasoningEvent, TextEvent, ToolDoneEvent, ToolStartEvent, UsageEvent
from mycode_cli.main import app, resolve_session_id, run_noninteractive
from mycode_cli.permissions import PERMISSION_DENIED_BY_USER_OUTPUT, PERMISSION_DENIED_OUTPUT
from mycode_cli.sessions import SessionStore

_TOOL_CALL = {"type": "tool_use", "id": "call-1", "name": "bash", "input": {"command": "rm -rf build"}}


class _BaseAgent:
    session_id = "session"
    model = "test-model"
    reasoning_effort = None


class _FakeAgent(_BaseAgent):
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
        yield UsageEvent(
            context_tokens=10,
            turn_usage={"input_tokens": 8, "output_tokens": 2},
            turn_cost={"total": 0.01},
            turn_duration_ms=5,
        )


class _ErrorAgent(_BaseAgent):
    async def achat(self, message: str, *, on_persist=None):
        yield ErrorEvent(message="provider error")


class _PermissionDeniedAgent(_BaseAgent):
    def __init__(self, output: str) -> None:
        self.output = output

    async def achat(self, message: str, *, on_persist=None):
        yield ToolStartEvent(tool_call=cast(Any, _TOOL_CALL))
        yield ToolDoneEvent(tool_use_id="call-1", output=self.output, is_error=True)


class _PermissionDeniedThenReplyAgent(_BaseAgent):
    async def achat(self, message: str, *, on_persist=None):
        yield ToolStartEvent(tool_call=cast(Any, _TOOL_CALL))
        yield ToolDoneEvent(tool_use_id="call-1", output=PERMISSION_DENIED_OUTPUT, is_error=True)
        if on_persist:
            await on_persist(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Permission was denied. Use --permission standard."}],
                }
            )


def _settings(tmp_path: Path) -> Any:
    return SimpleNamespace(cwd=str(tmp_path), permission=SimpleNamespace(level="safe"))


async def _run(agent: Any, tmp_path: Path, output_format: Any = "text") -> int:
    return await run_noninteractive(
        agent,
        "hello",
        store=SessionStore(tmp_path),
        settings=_settings(tmp_path),
        provider_name="test",
        output_format=output_format,
    )


class TestRunNoninteractive:
    @pytest.mark.asyncio
    async def test_prints_only_final_reply(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = await _run(_FakeAgent(), tmp_path)

        captured = capsys.readouterr()
        assert code == 0
        assert captured.out == "Persisted final answer\n"
        assert captured.err == ""

    @pytest.mark.asyncio
    async def test_prints_errors_to_stderr(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = await _run(_ErrorAgent(), tmp_path)

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert captured.err == "provider error\n"

    @pytest.mark.parametrize("output", [PERMISSION_DENIED_OUTPUT, PERMISSION_DENIED_BY_USER_OUTPUT])
    async def test_prints_permission_denials_to_stderr(
        self, output: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await _run(_PermissionDeniedAgent(output), tmp_path)

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert captured.err == f"{output}\n"

    @pytest.mark.asyncio
    async def test_prints_final_reply_after_permission_denial(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await _run(_PermissionDeniedThenReplyAgent(), tmp_path)

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == "Permission was denied. Use --permission standard.\n"
        assert captured.err == ""

    @pytest.mark.asyncio
    async def test_jsonl_streams_init_events_and_result(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await _run(_FakeAgent(), tmp_path, "jsonl")

        records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert code == 0
        assert [record["type"] for record in records] == ["init", "reasoning", "text", "usage", "result"]
        assert records[0]["session_id"] == "session"
        result = records[-1]
        assert result["is_error"] is False
        assert result["result"] == "Persisted final answer"
        assert result["usage"] == {"input_tokens": 8, "output_tokens": 2}
        assert result["cost"] == {"total": 0.01}

    @pytest.mark.asyncio
    async def test_json_reports_permission_denials(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = await _run(_PermissionDeniedThenReplyAgent(), tmp_path, "json")

        (line,) = capsys.readouterr().out.splitlines()
        result = json.loads(line)
        assert code == 1
        assert result["is_error"] is True
        assert result["error"] == PERMISSION_DENIED_OUTPUT
        assert result["result"] == "Permission was denied. Use --permission standard."
        assert result["permission_denials"] == [
            {"tool_use_id": "call-1", "name": "bash", "input": {"command": "rm -rf build"}}
        ]

    @pytest.mark.asyncio
    async def test_json_reports_session_write_failures(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class _ReadOnlyStore:
            async def record_user_turn(self, *args: Any, **kwargs: Any) -> None:
                raise OSError("Read-only file system")

        code = await run_noninteractive(
            cast(Any, _FakeAgent()),
            "hello",
            store=cast(Any, _ReadOnlyStore()),
            settings=_settings(tmp_path),
            provider_name="test",
            output_format="json",
        )

        result = json.loads(capsys.readouterr().out)
        assert code == 1
        assert result["is_error"] is True
        assert result["error"] == "Read-only file system"
        assert result["session_id"] == "session"


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str], stdin: str, expected: str
) -> None:
    from typer.testing import CliRunner

    import mycode_cli.main as main

    sent: list[str] = []

    async def fake_run(agent: Any, message: str, **kwargs: Any) -> int:
        sent.append(message)
        return 0

    monkeypatch.setattr(main, "build_agent", lambda **kwargs: None)
    monkeypatch.setattr(main, "run_noninteractive", fake_run)
    monkeypatch.setattr(
        main,
        "_bootstrap",
        lambda **kwargs: SimpleNamespace(
            store=SessionStore(tmp_path),
            settings=None,
            resolved_provider=SimpleNamespace(provider_name="test"),
            session_id="s",
        ),
    )

    result = CliRunner().invoke(app, ["run", *args], input=stdin)

    assert result.exit_code == 0, result.output
    assert sent == [expected]


def test_run_refuses_a_session_open_in_another_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    import mycode_cli.main as main

    store = SessionStore(tmp_path)
    monkeypatch.setattr(
        main,
        "_bootstrap",
        lambda **kwargs: SimpleNamespace(
            store=store, settings=None, resolved_provider=SimpleNamespace(provider_name="test"), session_id="s"
        ),
    )

    with store.lock("s", "the terminal UI"):
        result = CliRunner().invoke(app, ["run", "--format", "json", "hello"], input="")

    assert result.exit_code == 1
    record = json.loads(result.stdout)
    assert record["error"].startswith("session s is in use by the terminal UI (pid ")


def test_run_requires_a_prompt() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["run"], input="")

    assert result.exit_code == 2
    assert "provide a prompt" in result.output


def test_run_reports_startup_errors_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    import mycode_cli.main as main

    def fail(**kwargs: Any) -> Any:
        raise ValueError("Unknown session: nope")

    monkeypatch.setattr(main, "_bootstrap", fail)

    result = CliRunner().invoke(app, ["run", "--format", "json", "hello"], input="")

    assert result.exit_code == 1
    record = json.loads(result.stdout)
    assert record["is_error"] is True
    assert record["error"] == "Unknown session: nope"
    assert record["session_id"] is None
