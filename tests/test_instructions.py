"""Tests for AGENTS.md discovery."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mycode_cli.config import get_settings
from mycode_cli.system_prompt import discover_instruction_files, load_instructions_prompt


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("MYCODE_HOME", str(path / ".mycode"))
    return path


def test_prefers_native_global_agents_and_current_cwd_agents(tmp_path: Path, home: Path) -> None:
    project = tmp_path / "project"
    cwd = project / "apps" / "api"
    cwd.mkdir(parents=True)

    write_file(home / ".agents" / "AGENTS.md", "Global compat")
    write_file(home / ".mycode" / "AGENTS.md", "Global native")
    write_file(cwd / "AGENTS.md", "Current cwd")

    with patch("mycode_cli.system_prompt.Path.home", return_value=home):
        settings = get_settings(str(cwd))
        files = discover_instruction_files(str(cwd), settings)
        prompt = load_instructions_prompt(str(cwd), settings)

    assert [str(path.resolve()) for path in files] == [
        str((home / ".mycode" / "AGENTS.md").resolve()),
        str((cwd / "AGENTS.md").resolve()),
    ]
    assert "Global native" in prompt
    assert "Current cwd" in prompt
    assert "Global compat" not in prompt


def test_loads_project_agents_from_project_to_cwd(tmp_path: Path, home: Path) -> None:
    project = tmp_path / "project"
    cwd = project / "apps" / "api"
    cwd.mkdir(parents=True)
    (project / ".git").mkdir()
    write_file(project / "AGENTS.md", "Parent project")
    write_file(cwd.parent / "AGENTS.md", "Nested project")
    write_file(cwd / "AGENTS.md", "Current cwd")

    with patch("mycode_cli.system_prompt.Path.home", return_value=home):
        files = discover_instruction_files(str(cwd))
        prompt = load_instructions_prompt(str(cwd))

    assert [str(path.resolve()) for path in files] == [
        str((project / "AGENTS.md").resolve()),
        str((cwd.parent / "AGENTS.md").resolve()),
        str((cwd / "AGENTS.md").resolve()),
    ]
    assert prompt.index("Parent project") < prompt.index("Nested project") < prompt.index("Current cwd")


def test_does_not_load_parent_agents_when_no_git_is_found(tmp_path: Path, home: Path) -> None:
    project = tmp_path / "project"
    cwd = project / "apps" / "api"
    cwd.mkdir(parents=True)
    write_file(project / "AGENTS.md", "Parent project")

    with patch("mycode_cli.system_prompt.Path.home", return_value=home):
        prompt = load_instructions_prompt(str(cwd))

    assert "Parent project" not in prompt


def test_uses_compat_global_agents_when_native_is_missing(tmp_path: Path, home: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_file(home / ".agents" / "AGENTS.md", "Compat global")

    with patch("mycode_cli.system_prompt.Path.home", return_value=home):
        prompt = load_instructions_prompt(str(workspace))

    assert "Compat global" in prompt
