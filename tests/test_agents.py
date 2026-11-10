"""Tests for subagent definitions and their discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycode_cli.agents import GENERAL_AGENT, AgentDefinition, discover_agents, format_agents
from mycode_cli.config import PermissionConfig


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home" / ".mycode"
    monkeypatch.setenv("MYCODE_HOME", str(home))
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    (path / ".git").mkdir(parents=True)
    return path


def test_general_is_built_in(home: Path, project: Path) -> None:
    assert discover_agents(str(project)) == [GENERAL_AGENT]


def test_parses_frontmatter_fields_and_body(home: Path, project: Path) -> None:
    path = project / ".mycode" / "agents" / "reviewer.md"
    write_file(
        path,
        "---\ndescription: |\n  Reviews a diff\n  for bugs\nmodel: openai/gpt-5.5\npermission: readonly\nmax_turns: 30\n---\n"
        "You review code.\n",
    )

    [*_, reviewer] = discover_agents(str(project))

    assert reviewer.name == "reviewer"
    assert reviewer.description == "Reviews a diff for bugs"
    assert reviewer.body == "You review code."
    assert reviewer.model == "openai/gpt-5.5"
    # The shorthand sets the level; the mode stays the parent's.
    assert reviewer.permission == PermissionConfig(level="readonly", mode="ask")
    assert reviewer.max_turns == 30


def test_permission_defaults_to_no_narrowing_and_takes_the_object_form(home: Path, project: Path) -> None:
    write_file(project / ".mycode" / "agents" / "a.md", "---\ndescription: d\n---\n")
    write_file(project / ".mycode" / "agents" / "b.md", "---\ndescription: d\npermission:\n  mode: deny\n---\n")

    [a, b, _general] = discover_agents(str(project))

    assert a.permission == PermissionConfig(level="yolo", mode="ask")
    assert b.permission == PermissionConfig(level="yolo", mode="deny")


def test_nearer_roots_override_by_name_and_results_are_sorted(home: Path, project: Path) -> None:
    write_file(home / "agents" / "zeta.md", "---\ndescription: user zeta\n---\n")
    write_file(home / "agents" / "docs.md", "---\ndescription: user docs\n---\n")
    write_file(project / ".mycode" / "agents" / "docs.md", "---\ndescription: project docs\n---\n")
    write_file(project / "app" / ".mycode" / "agents" / "other.md", "---\nname: docs\ndescription: app docs\n---\n")

    agents = discover_agents(str(project / "app"))

    assert [(agent.name, agent.description) for agent in agents] == [
        ("docs", "app docs"),
        ("general", GENERAL_AGENT.description),
        ("zeta", "user zeta"),
    ]


def test_a_definition_file_replaces_the_built_in_general(home: Path, project: Path) -> None:
    write_file(home / "agents" / "general.md", "---\ndescription: mine\nmodel: other/cheap\n---\n")

    assert discover_agents(str(project)) == [AgentDefinition(name="general", description="mine", model="other/cheap")]


@pytest.mark.parametrize(
    "content",
    [
        "no frontmatter",
        "---\nname: ok\n---\n",
        "---\ndescription: '  '\n---\n",
        "---\nname: bad name\ndescription: d\n---\n",
        "---\ndescription: d\npermission: root\n---\n",
        "---\ndescription: d\npermission: [readonly]\n---\n",
        "---\ndescription: d\nmax_turns: 0\n---\n",
        "---\ndescription: d\nmax_turns: true\n---\n",
        "---\ndescription: d\nmodel: 3\n---\n",
    ],
)
def test_invalid_definitions_are_skipped(home: Path, project: Path, content: str) -> None:
    write_file(project / ".mycode" / "agents" / "broken.md", content)

    assert discover_agents(str(project)) == [GENERAL_AGENT]


def test_format_lists_name_and_description() -> None:
    reviewer = AgentDefinition(name="reviewer", description="Reviews diffs")

    assert format_agents([GENERAL_AGENT, reviewer]) == (
        f"- general: {GENERAL_AGENT.description}\n- reviewer: Reviews diffs"
    )
