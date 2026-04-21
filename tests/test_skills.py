"""Tests for skill discovery and prompt formatting."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycode_cli.system_prompt import (
    _parse_skill_md,
    _scan_skill_root,
    discover_skills,
    load_skills_prompt,
)


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def skill_text(*, name: str | None = "test-skill", description: str | None = "A test skill.") -> str:
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    if description is not None:
        lines.append(f"description: {description}")
    lines.extend(["---", "", "Body."])
    return "\n".join(lines)


@pytest.fixture
def skill_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
    monkeypatch.setattr("mycode_cli.system_prompt.Path.home", lambda: home)
    return home


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


class TestParseSkillMd:
    def test_accepts_valid_frontmatter(self, tmp_path: Path) -> None:
        path = tmp_path / "SKILL.md"
        write_file(path, skill_text())

        skill = _parse_skill_md(path, "project")

        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test skill."
        assert skill.source == "project"

    def test_uses_fallback_name_when_frontmatter_omits_name(self, tmp_path: Path) -> None:
        path = tmp_path / "SKILL.md"
        write_file(path, skill_text(name=None, description="Fallback skill."))

        skill = _parse_skill_md(path, "project", fallback_name="cool-tool")

        assert skill is not None
        assert skill.name == "cool-tool"
        assert skill.description == "Fallback skill."

    @pytest.mark.parametrize(
        "content",
        [
            skill_text(description=None),
            skill_text(name="bad name!"),
            "# Just a markdown file\nNo YAML frontmatter here.\n",
        ],
    )
    def test_rejects_invalid_skill_files(self, tmp_path: Path, content: str) -> None:
        path = tmp_path / "SKILL.md"
        write_file(path, content)

        assert _parse_skill_md(path, "global") is None

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert _parse_skill_md(tmp_path / "missing.md", "global") is None


class TestScanSkillRoot:
    def test_discovers_supported_layouts_and_ignores_noise(self, tmp_path: Path) -> None:
        write_file(tmp_path / "deploy.md", skill_text(name="deploy", description="Deploy things."))
        write_file(tmp_path / "lint.md", skill_text(name="lint", description="Lint things."))
        write_file(tmp_path / "nested" / "SKILL.md", skill_text(description="Nested skill."))
        write_file(tmp_path / "cool-tool" / "SKILL.md", skill_text(name=None, description="Fallback skill."))
        write_file(tmp_path / ".hidden" / "SKILL.md", skill_text(description="Hidden skill."))
        write_file(tmp_path / "node_modules" / "pkg" / "SKILL.md", skill_text(description="Ignored skill."))
        write_file(
            tmp_path / "a" / "b" / "c" / "SKILL.md", skill_text(name="depth-three", description="Allowed depth.")
        )
        write_file(tmp_path / "a" / "b" / "c" / "d" / "SKILL.md", skill_text(name="too-deep", description="Too deep."))

        skills = _scan_skill_root(tmp_path, "project")

        assert {skill.name for skill in skills} == {
            "cool-tool",
            "deploy",
            "depth-three",
            "lint",
            "test-skill",
        }


class TestDiscoverSkills:
    def test_prefers_current_native_root_over_compat_and_global(self, skill_home: Path, workspace: Path) -> None:
        write_file(
            skill_home / ".agents" / "skills" / "shared" / "SKILL.md",
            skill_text(name="shared", description="Compat global."),
        )
        write_file(
            skill_home / ".mycode" / "skills" / "shared" / "SKILL.md",
            skill_text(name="shared", description="Native global."),
        )
        write_file(
            workspace / ".agents" / "skills" / "shared" / "SKILL.md",
            skill_text(name="shared", description="Compat project."),
        )
        write_file(
            workspace / ".mycode" / "skills" / "shared" / "SKILL.md",
            skill_text(name="shared", description="Native project."),
        )

        skills = discover_skills(str(workspace))

        assert len(skills) == 1
        assert skills[0].description == "Native project."
        assert skills[0].source == "project"

    def test_does_not_load_parent_skill_roots_from_nested_cwd(self, tmp_path: Path, skill_home: Path) -> None:
        project = tmp_path / "project"
        nested_cwd = project / "apps" / "api"
        nested_cwd.mkdir(parents=True)
        write_file(
            project / ".mycode" / "skills" / "shared" / "SKILL.md",
            skill_text(name="shared", description="Parent project skill."),
        )

        skills = discover_skills(str(nested_cwd))

        assert skills == []


class TestLoadSkillsPrompt:
    def test_formats_discovered_skills_into_prompt(self, skill_home: Path, workspace: Path) -> None:
        write_file(
            skill_home / ".mycode" / "skills" / "greet" / "SKILL.md",
            skill_text(name="greet", description="Greeting skill."),
        )

        result = load_skills_prompt(str(workspace))

        assert "<available_skills>" in result
        assert "<name>greet</name>" in result
        assert "<description>Greeting skill.</description>" in result
