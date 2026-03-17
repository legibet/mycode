"""Tests for AGENTS.md discovery."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from mycode.core.config import get_settings
from mycode.core.instructions import discover_instruction_files, load_instructions_prompt


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestInstructions:
    def test_prefers_mycode_global_agents_and_project_root_agents(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        project = tmp_path / "project"
        cwd = project / "apps" / "api"
        cwd.mkdir(parents=True)
        (project / ".git").mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))

        _write(home / ".agents" / "AGENTS.md", "Global compat")
        _write(home / ".mycode" / "AGENTS.md", "Global native")
        _write(project / "AGENTS.md", "Project root")

        with patch("mycode.core.instructions.Path.home", return_value=home):
            settings = get_settings(str(cwd))
            files = discover_instruction_files(str(cwd), settings)
            prompt = load_instructions_prompt(str(cwd), settings)

        assert [str(path.resolve()) for path in files] == [
            str((home / ".mycode" / "AGENTS.md").resolve()),
            str((project / "AGENTS.md").resolve()),
        ]
        assert "Global native" in prompt
        assert "Project root" in prompt
        assert "Global compat" not in prompt

    def test_uses_agents_global_when_mycode_missing(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        _write(home / ".agents" / "AGENTS.md", "Compat global")

        with patch("mycode.core.instructions.Path.home", return_value=home):
            prompt = load_instructions_prompt(str(workspace))

        assert "Compat global" in prompt

    def test_truncates_by_max_bytes(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        _write(home / ".mycode" / "AGENTS.md", "0123456789abcdef")

        with patch("mycode.core.instructions.Path.home", return_value=home):
            monkeypatch.setattr("mycode.core.instructions._MAX_INSTRUCTION_BYTES", 12)
            prompt = load_instructions_prompt(str(workspace))

        assert "0123456789ab" in prompt
        assert "cdef" not in prompt
        assert "[Truncated due to instruction size limit.]" in prompt
