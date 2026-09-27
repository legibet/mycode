"""Tests for the CLI entrypoint: non-interactive runs, session resolution, and commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from mycode.agent import Event
from mycode_cli.config import Settings
from mycode_cli.main import app, resolve_session, run_noninteractive
from mycode_cli.permissions import PERMISSION_DENIED_BY_USER_OUTPUT, PERMISSION_DENIED_OUTPUT
from mycode_cli.runtime import load_session_cost
from mycode_cli.sessions import SessionStore


class _FakeAgent:
    provider = "anthropic"
    model = "claude-sonnet-4-6"
    api_base = None
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
        yield Event("reasoning", {"delta": "Hidden reasoning"})
        yield Event("text", {"delta": "Streamed answer should stay hidden"})


class _ErrorAgent:
    session_id = "session"

    async def achat(self, message: str, *, on_persist=None):
        yield Event("error", {"message": "provider error"})


class _PermissionDeniedAgent:
    session_id = "session"

    def __init__(self, output: str) -> None:
        self.output = output

    async def achat(self, message: str, *, on_persist=None):
        yield Event("tool_done", {"tool_use_id": "call-1", "output": self.output, "is_error": True})


class _PermissionDeniedThenReplyAgent:
    session_id = "session"

    async def achat(self, message: str, *, on_persist=None):
        yield Event("tool_done", {"tool_use_id": "call-1", "output": PERMISSION_DENIED_OUTPUT, "is_error": True})
        if on_persist:
            await on_persist(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Permission was denied. Use --permission standard."}],
                }
            )


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
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda cwd: Settings(
            providers={},
            default_provider=None,
            default_model=None,
            port=8000,
            cwd=cwd,
            project=cwd,
            config_paths=[],
        ),
    )
    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = CliRunner().invoke(app, ["web", "--dev", "--port", "8765"])

    assert result.exit_code == 0, result.output
    assert run_args["app_ref"] == "mycode_cli.server.app:create_api_app"
    assert run_args["reload"] is True
    assert run_args["factory"] is True
