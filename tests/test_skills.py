"""Tests for skill discovery and prompt formatting."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycode_cli.system_prompt import (
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


def test_discovers_supported_layouts_and_ignores_invalid_skill_files(skill_home: Path, workspace: Path) -> None:
    root = workspace / ".mycode" / "skills"
    write_file(root / "deploy.md", skill_text(name="deploy", description="Deploy things."))
    write_file(root / "lint.md", skill_text(name="lint", description="Lint things."))
    write_file(root / "nested" / "SKILL.md", skill_text(description="Nested skill."))
    write_file(root / "cool-tool" / "SKILL.md", skill_text(name=None, description="Fallback skill."))
    write_file(root / ".hidden" / "SKILL.md", skill_text(description="Hidden skill."))
    write_file(root / "node_modules" / "pkg" / "SKILL.md", skill_text(description="Ignored skill."))
    write_file(root / "invalid" / "SKILL.md", skill_text(description=None))
    write_file(root / "bad name!" / "SKILL.md", skill_text(name="bad name!", description="Bad skill."))
    write_file(root / "plain" / "SKILL.md", "# Just a markdown file\nNo YAML frontmatter here.\n")
    write_file(root / "a" / "b" / "c" / "SKILL.md", skill_text(name="depth-three", description="Allowed depth."))
    write_file(root / "a" / "b" / "c" / "d" / "SKILL.md", skill_text(name="too-deep", description="Too deep."))
    linked_skill = workspace / "linked-skill" / "SKILL.md"
    write_file(linked_skill, skill_text(name="linked", description="Linked skill."))
    (root / "linked").symlink_to(linked_skill.parent, target_is_directory=True)

    skills = discover_skills(str(workspace))

    assert {skill.name for skill in skills} == {
        "cool-tool",
        "deploy",
        "depth-three",
        "lint",
        "linked",
        "test-skill",
    }
    assert all(skill.source == "project" for skill in skills)


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

    def test_loads_project_skill_roots_from_project_to_cwd(self, tmp_path: Path, skill_home: Path) -> None:
        project = tmp_path / "project"
        nested_cwd = project / "apps" / "api"
        nested_cwd.mkdir(parents=True)
        (project / ".git").mkdir()
        write_file(
            project / ".agents" / "skills" / "parent" / "SKILL.md",
            skill_text(name="parent", description="Compat parent project skill."),
        )
        write_file(
            project / ".mycode" / "skills" / "shared" / "SKILL.md",
            skill_text(name="shared", description="Parent project skill."),
        )
        write_file(
            nested_cwd / ".mycode" / "skills" / "shared" / "SKILL.md",
            skill_text(name="shared", description="Nearest project skill."),
        )

        skills = discover_skills(str(nested_cwd))

        assert {skill.name for skill in skills} == {"parent", "shared"}
        assert [skill for skill in skills if skill.name == "shared"][0].description == "Nearest project skill."

    def test_does_not_load_parent_skill_roots_when_no_git_is_found(self, tmp_path: Path, skill_home: Path) -> None:
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
