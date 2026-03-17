"""Tests for CLI output behavior."""

from mycode.cli import run_once
from mycode.core.agent import Event


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


async def test_run_once_ignores_reasoning_output(monkeypatch):
    fake_console = _FakeConsole()
    monkeypatch.setattr("mycode.cli.console", fake_console)

    code = await run_once(
        _FakeAgent(),
        store=_FakeStore(),
        session_id="test-session",
        message="hello",
    )

    assert code == 0
    printed = [str(args[0]) for args, _kwargs in fake_console.calls if args]
    assert "Hidden reasoning" not in printed
    assert "Visible answer" in printed
