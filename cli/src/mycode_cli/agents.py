"""Subagent definitions: the built-in ``general`` agent plus markdown files
from ``~/.mycode/agents/`` and ``<project>/.mycode/agents/``.

A definition file is markdown with YAML frontmatter; its body is appended to
the child's system prompt::

    ---
    description: Reviews a diff for correctness bugs
    model: anthropic/claude-sonnet-5-5
    permission: readonly
    max_turns: 30
    ---
    You review code changes. Report bugs with file and line.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from mycode_cli.config import PermissionConfig, parse_permission, project_dirs, resolve_mycode_home
from mycode_cli.system_prompt import parse_frontmatter

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# A definition's permission narrows the parent's; this one narrows nothing.
_NO_LIMIT = PermissionConfig(level="yolo", mode="ask")


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    # One line, listed in the agent tool's description.
    description: str
    # Appended to the child's system prompt.
    body: str = ""
    # A ``find_model`` reference; ``None`` runs the first usable ``subagent.models`` entry, else the parent's model.
    model: str | None = None
    # The strictest permission the child runs at, combined with the parent's.
    permission: PermissionConfig = _NO_LIMIT
    max_turns: int | None = None


GENERAL_AGENT = AgentDefinition(
    name="general",
    description="General-purpose agent with the same tools as you.",
)


def discover_agents(cwd: str) -> list[AgentDefinition]:
    """Return the built-in ``general`` agent and the user and project definitions, sorted by name.

    Later roots win by name, so a user definition overrides the built-in, a
    project definition overrides a user one, and nearer project directories
    override farther ones.
    """

    roots = [resolve_mycode_home() / "agents"]
    roots.extend(directory / ".mycode" / "agents" for directory in project_dirs(cwd))

    found = {GENERAL_AGENT.name: GENERAL_AGENT}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            if (definition := _parse_definition(path)) is not None:
                found[definition.name] = definition
    return sorted(found.values(), key=lambda definition: definition.name)


def _parse_definition(path: Path) -> AgentDefinition | None:
    """Parse one definition file; an invalid one is logged and skipped."""

    try:
        parsed = parse_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        logger.warning("Failed to read agent file: %s", path)
        return None
    if parsed is None:
        logger.warning("Agent file has no valid frontmatter: %s", path)
        return None
    frontmatter, body = parsed

    name = frontmatter.get("name", path.stem)
    if not isinstance(name, str) or not _NAME_RE.match(name.strip()):
        logger.warning("Agent file has an invalid name: %s", path)
        return None
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        logger.warning("Agent file has no description: %s", path)
        return None
    model = frontmatter.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        logger.warning("Agent file has an invalid model: %s", path)
        return None
    try:
        permission = parse_permission(frontmatter.get("permission"), _NO_LIMIT)
    except ValueError:
        logger.warning("Agent file has an invalid permission: %s", path)
        return None
    max_turns = frontmatter.get("max_turns")
    if max_turns is not None and (isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1):
        logger.warning("Agent file has an invalid max_turns: %s", path)
        return None

    return AgentDefinition(
        name=name.strip(),
        description=" ".join(description.split()),
        body=body,
        model=model.strip() if model is not None else None,
        permission=permission,
        max_turns=max_turns,
    )


def format_agents(definitions: list[AgentDefinition]) -> str:
    """List agents as ``- name: description`` lines for the agent tool's description."""

    return "\n".join(f"- {definition.name}: {definition.description}" for definition in definitions)
