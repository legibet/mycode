from pathlib import Path

from mycode_cli.tui.state import load_efforts, save_efforts


def test_tui_efforts_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))

    save_efforts({"openai/gpt-5": "high", "anthropic/claude": "auto"})

    assert load_efforts() == {"openai/gpt-5": "high", "anthropic/claude": "auto"}


def test_tui_efforts_ignore_invalid_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    (tmp_path / "tui.json").write_text('{"effort": ["invalid"]}', encoding="utf-8")

    assert load_efforts() == {}
