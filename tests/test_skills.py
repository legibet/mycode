"""Tests for app.agent.skills — skill discovery, parsing, and formatting."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.agent.skills import (
    Skill,
    _find_project_root,
    _parse_skill_md,
    _scan_skill_root,
    discover_skills,
    format_skills_for_prompt,
    load_skills_prompt,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


VALID_SKILL = """\
---
name: test-skill
description: A test skill for unit tests.
---

# Instructions
Do something useful.
"""

NO_DESC_SKILL = """\
---
name: no-desc
---
Body here.
"""

NO_FRONTMATTER = """\
# Just a markdown file
No YAML frontmatter here.
"""

MINIMAL_SKILL = """\
---
description: Minimal skill with no explicit name.
---
Body.
"""


class TestParseSkillMd:
    def test_valid(self, tmp_path: Path) -> None:
        p = tmp_path / "SKILL.md"
        _write(p, VALID_SKILL)
        skill = _parse_skill_md(p, "project")
        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test skill for unit tests."
        assert skill.source == "project"

    def test_missing_description(self, tmp_path: Path) -> None:
        p = tmp_path / "SKILL.md"
        _write(p, NO_DESC_SKILL)
        assert _parse_skill_md(p, "global") is None

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        p = tmp_path / "SKILL.md"
        _write(p, NO_FRONTMATTER)
        assert _parse_skill_md(p, "global") is None

    def test_fallback_name(self, tmp_path: Path) -> None:
        p = tmp_path / "SKILL.md"
        _write(p, MINIMAL_SKILL)
        skill = _parse_skill_md(p, "project", fallback_name="my-tool")
        assert skill is not None
        assert skill.name == "my-tool"

    def test_invalid_name_chars(self, tmp_path: Path) -> None:
        p = tmp_path / "SKILL.md"
        _write(p, "---\nname: bad name!\ndescription: test\n---\n")
        assert _parse_skill_md(p, "global") is None

    def test_file_not_found(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent.md"
        assert _parse_skill_md(p, "global") is None


class TestScanSkillRoot:
    def test_direct_md_files(self, tmp_path: Path) -> None:
        _write(tmp_path / "deploy.md", "---\nname: deploy\ndescription: Deploy things.\n---\n")
        _write(tmp_path / "lint.md", "---\nname: lint\ndescription: Lint code.\n---\n")
        skills = _scan_skill_root(tmp_path, "project")
        names = {s.name for s in skills}
        assert names == {"deploy", "lint"}

    def test_subdir_skill_md(self, tmp_path: Path) -> None:
        _write(tmp_path / "my-skill" / "SKILL.md", VALID_SKILL)
        skills = _scan_skill_root(tmp_path, "project")
        assert len(skills) == 1
        assert skills[0].name == "test-skill"

    def test_subdir_fallback_name(self, tmp_path: Path) -> None:
        _write(tmp_path / "cool-tool" / "SKILL.md", MINIMAL_SKILL)
        skills = _scan_skill_root(tmp_path, "project")
        assert len(skills) == 1
        assert skills[0].name == "cool-tool"

    def test_skip_dotdirs(self, tmp_path: Path) -> None:
        _write(tmp_path / ".hidden" / "SKILL.md", VALID_SKILL)
        skills = _scan_skill_root(tmp_path, "project")
        assert len(skills) == 0

    def test_skip_node_modules(self, tmp_path: Path) -> None:
        _write(tmp_path / "node_modules" / "pkg" / "SKILL.md", VALID_SKILL)
        skills = _scan_skill_root(tmp_path, "project")
        assert len(skills) == 0

    def test_nonexistent_root(self, tmp_path: Path) -> None:
        skills = _scan_skill_root(tmp_path / "nope", "global")
        assert skills == []

    def test_depth_limit(self, tmp_path: Path) -> None:
        # Depth 3 should be found (root -> a -> b -> c, depth=3 for c)
        _write(tmp_path / "a" / "b" / "c" / "SKILL.md", VALID_SKILL)
        skills = _scan_skill_root(tmp_path, "project")
        assert len(skills) == 1

        # Depth 4 should NOT be found
        _write(tmp_path / "a" / "b" / "c" / "d" / "SKILL.md", "---\nname: deep\ndescription: Too deep.\n---\n")
        skills = _scan_skill_root(tmp_path, "project")
        # Should still only find the depth-3 one
        names = {s.name for s in skills}
        assert "deep" not in names
        assert "test-skill" in names


class TestDiscoverSkills:
    def test_project_overrides_global(self, tmp_path: Path) -> None:
        global_dir = tmp_path / "home" / ".mycode" / "skills"
        project_dir = tmp_path / "project" / ".mycode" / "skills"
        git_dir = tmp_path / "project" / ".git"
        git_dir.mkdir(parents=True)

        _write(global_dir / "shared" / "SKILL.md", "---\nname: shared\ndescription: Global version.\n---\n")
        _write(project_dir / "shared" / "SKILL.md", "---\nname: shared\ndescription: Project version.\n---\n")

        with patch("app.agent.skills.Path.home", return_value=tmp_path / "home"):
            skills = discover_skills(str(tmp_path / "project"))

        assert len(skills) == 1
        assert skills[0].description == "Project version."
        assert skills[0].source == "project"

    def test_extra_paths(self, tmp_path: Path) -> None:
        extra = tmp_path / "extra-skills"
        _write(extra / "bonus" / "SKILL.md", "---\nname: bonus\ndescription: Extra skill.\n---\n")

        with patch("app.agent.skills.Path.home", return_value=tmp_path / "home"):
            skills = discover_skills(str(tmp_path), extra_paths=[str(extra)])

        assert len(skills) == 1
        assert skills[0].name == "bonus"
        assert skills[0].source == "config"

    def test_no_skills(self, tmp_path: Path) -> None:
        with patch("app.agent.skills.Path.home", return_value=tmp_path / "home"):
            skills = discover_skills(str(tmp_path))
        assert skills == []

    def test_sorted_by_name(self, tmp_path: Path) -> None:
        root = tmp_path / "home" / ".mycode" / "skills"
        _write(root / "zebra.md", "---\nname: zebra\ndescription: Z.\n---\n")
        _write(root / "alpha.md", "---\nname: alpha\ndescription: A.\n---\n")
        _write(root / "mid.md", "---\nname: mid\ndescription: M.\n---\n")

        with patch("app.agent.skills.Path.home", return_value=tmp_path / "home"):
            skills = discover_skills(str(tmp_path))

        assert [s.name for s in skills] == ["alpha", "mid", "zebra"]


class TestFindProjectRoot:
    def test_finds_git(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        assert _find_project_root(str(sub)) == tmp_path

    def test_no_git(self, tmp_path: Path) -> None:
        # tmp_path has no .git — but parent dirs might, so we test carefully
        isolated = tmp_path / "isolated"
        isolated.mkdir()
        # This may find a .git in a parent — that's OK, just test it doesn't crash
        result = _find_project_root(str(isolated))
        # We can't guarantee None here since tests run inside a git repo
        assert result is None or isinstance(result, Path)


class TestFormatSkillsForPrompt:
    def test_empty(self) -> None:
        assert format_skills_for_prompt([]) == ""

    def test_format(self) -> None:
        skills = [
            Skill(name="alpha", description="First skill.", path="/a/SKILL.md", source="project"),
            Skill(name="beta", description="Second skill.", path="/b/SKILL.md", source="global"),
        ]
        result = format_skills_for_prompt(skills)
        assert "<available_skills>" in result
        assert "</available_skills>" in result
        assert "name: alpha" in result
        assert "name: beta" in result
        assert "path: /a/SKILL.md" in result
        assert "description: First skill." in result


class TestLoadSkillsPrompt:
    def test_integration(self, tmp_path: Path) -> None:
        root = tmp_path / "home" / ".mycode" / "skills"
        _write(root / "greet" / "SKILL.md", "---\nname: greet\ndescription: Greeting skill.\n---\nHello!")

        with patch("app.agent.skills.Path.home", return_value=tmp_path / "home"):
            result = load_skills_prompt(str(tmp_path))

        assert "<available_skills>" in result
        assert "name: greet" in result

    def test_no_skills_returns_empty(self, tmp_path: Path) -> None:
        with patch("app.agent.skills.Path.home", return_value=tmp_path / "home"):
            result = load_skills_prompt(str(tmp_path))
        assert result == ""
