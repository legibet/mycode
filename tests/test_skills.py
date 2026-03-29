"""Tests for system_prompt skill discovery and formatting."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from mycode.core.system_prompt import (
    _parse_skill_md,
    _scan_skill_root,
    discover_skills,
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
    def test_mycode_overrides_agents_for_same_scope(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        monkeypatch.setenv("MYCODE_HOME", str(home / ".mycode"))
        _write(
            home / ".agents" / "skills" / "shared" / "SKILL.md",
            "---\nname: shared\ndescription: Compat version.\n---\n",
        )
        _write(
            home / ".mycode" / "skills" / "shared" / "SKILL.md",
            "---\nname: shared\ndescription: Native version.\n---\n",
        )

        with patch("mycode.core.system_prompt.Path.home", return_value=home):
            skills = discover_skills(str(tmp_path / "workspace"))

        assert len(skills) == 1
        assert skills[0].description == "Native version."
        assert skills[0].source == "global"

    def test_project_overrides_global(self, tmp_path: Path, monkeypatch) -> None:
        global_dir = tmp_path / "home" / ".mycode" / "skills"
        project_dir = tmp_path / "project" / ".mycode" / "skills"
        monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home" / ".mycode"))

        _write(global_dir / "shared" / "SKILL.md", "---\nname: shared\ndescription: Global version.\n---\n")
        _write(project_dir / "shared" / "SKILL.md", "---\nname: shared\ndescription: Project version.\n---\n")

        with patch("mycode.core.system_prompt.Path.home", return_value=tmp_path / "home"):
            skills = discover_skills(str(tmp_path / "project"))

        assert len(skills) == 1
        assert skills[0].description == "Project version."
        assert skills[0].source == "project"

    def test_current_cwd_mycode_skills_apply(self, tmp_path: Path, monkeypatch) -> None:
        nested_dir = tmp_path / "project" / "apps" / "api"
        nested_dir.mkdir(parents=True)
        monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home" / ".mycode"))

        _write(
            nested_dir / ".mycode" / "skills" / "shared" / "SKILL.md",
            "---\nname: shared\ndescription: Current cwd version.\n---\n",
        )

        with patch("mycode.core.system_prompt.Path.home", return_value=tmp_path / "home"):
            skills = discover_skills(str(nested_dir))

        assert len(skills) == 1
        assert skills[0].description == "Current cwd version."
        assert skills[0].source == "project"

    def test_current_cwd_agents_skills_apply(self, tmp_path: Path, monkeypatch) -> None:
        nested_dir = tmp_path / "project" / "apps" / "api"
        nested_dir.mkdir(parents=True)
        monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home" / ".mycode"))

        _write(
            nested_dir / ".agents" / "skills" / "shared" / "SKILL.md",
            "---\nname: shared\ndescription: Current cwd compat version.\n---\n",
        )

        with patch("mycode.core.system_prompt.Path.home", return_value=tmp_path / "home"):
            skills = discover_skills(str(nested_dir))

        assert len(skills) == 1
        assert skills[0].description == "Current cwd compat version."
        assert skills[0].source == "project"

    def test_parent_skills_do_not_apply_from_nested_cwd(self, tmp_path: Path, monkeypatch) -> None:
        project = tmp_path / "project"
        nested_dir = project / "apps" / "api"
        nested_dir.mkdir(parents=True)
        monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home" / ".mycode"))

        _write(
            project / ".mycode" / "skills" / "shared" / "SKILL.md",
            "---\nname: shared\ndescription: Parent version.\n---\n",
        )

        with patch("mycode.core.system_prompt.Path.home", return_value=tmp_path / "home"):
            skills = discover_skills(str(nested_dir))

        assert skills == []

    def test_no_skills(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home" / ".mycode"))
        with patch("mycode.core.system_prompt.Path.home", return_value=tmp_path / "home"):
            skills = discover_skills(str(tmp_path))
        assert skills == []


class TestLoadSkillsPrompt:
    def test_integration(self, tmp_path: Path, monkeypatch) -> None:
        root = tmp_path / "home" / ".mycode" / "skills"
        monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home" / ".mycode"))
        _write(root / "greet" / "SKILL.md", "---\nname: greet\ndescription: Greeting skill.\n---\nHello!")

        with patch("mycode.core.system_prompt.Path.home", return_value=tmp_path / "home"):
            result = load_skills_prompt(str(tmp_path))

        assert "<available_skills>" in result
        assert "name: greet" in result

    def test_no_skills_returns_empty(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "home" / ".mycode"))
        with patch("mycode.core.system_prompt.Path.home", return_value=tmp_path / "home"):
            result = load_skills_prompt(str(tmp_path))
        assert result == ""
