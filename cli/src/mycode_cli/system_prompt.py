"""System prompt construction.

This module owns the full runtime system prompt:

- base prompt text (inlined below as _BASE_PROMPT)
- project instructions from AGENTS.md
- available skills from SKILL.md files
"""

from __future__ import annotations

import html
import logging
import re
from collections import deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from mycode.messages import ContentBlock, text_block
from mycode_cli.config import Settings, get_settings, project_dirs, resolve_mycode_home, resolve_project

logger = logging.getLogger(__name__)

_MAX_SCAN_DEPTH = 3
_MAX_DIRS_PER_ROOT = 200
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git"})
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_NAME_MAX_LEN = 64
_DESCRIPTION_MAX_LEN = 1024  # Agent Skills spec limit
_SKILLS_PROMPT_WARN_CHARS = 16_000  # ~4k tokens; the catalog is always loaded into the system prompt
_BUILTIN_SLASH_NAMES = ("clear", "compact", "new", "resume", "rewind", "provider", "model", "effort", "q")
_RESERVED_SLASH_NAMES = frozenset(name[:length] for name in _BUILTIN_SLASH_NAMES for length in range(1, len(name) + 1))

_BASE_PROMPT = """\
You are mycode, a coding agent working in the user's workspace.

- Use read to inspect existing files before editing them.
- Use bash to explore the workspace and run commands.
- Use edit for targeted changes and write only for new files or complete rewrites.
- Be concise and relevant.\
"""


# ---------------------------------------------------------------------
# Full system prompt assembly
# ---------------------------------------------------------------------


def build_system_prompt(cwd: str, settings: Settings | None = None) -> str:
    """Build the full runtime system prompt for the current directory."""

    resolved_cwd = str(Path(cwd).resolve(strict=False))
    resolved_settings = settings or get_settings(resolved_cwd)

    parts = [_BASE_PROMPT]

    instructions_prompt = load_instructions_prompt(resolved_cwd, resolved_settings)
    if instructions_prompt:
        parts.append(instructions_prompt)

    skills_prompt = load_skills_prompt(resolved_cwd)
    if skills_prompt:
        parts.append(skills_prompt)

    parts.append(f"Current working directory: {resolved_cwd}\nCurrent date: {date.today().strftime('%Y-%m')}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------
# Project instructions from AGENTS.md
# ---------------------------------------------------------------------


def discover_instruction_files(cwd: str, settings: Settings | None = None) -> list[Path]:
    """Discover global AGENTS.md and project AGENTS.md files."""

    resolved_cwd = settings.cwd if settings else cwd
    project = settings.project if settings else str(resolve_project(resolved_cwd))
    home = Path.home().resolve(strict=False)
    mycode_home = resolve_mycode_home()
    files: list[Path] = []

    global_candidate = mycode_home / "AGENTS.md"
    compat_global_candidate = home / ".agents" / "AGENTS.md"
    if global_candidate.is_file():
        files.append(global_candidate)
    elif compat_global_candidate.is_file():
        files.append(compat_global_candidate)

    for directory in project_dirs(resolved_cwd, project):
        local_candidate = directory / "AGENTS.md"
        if local_candidate.is_file():
            files.append(local_candidate)

    return files


def load_instructions_prompt(cwd: str, settings: Settings | None = None) -> str:
    """Load AGENTS.md files into one prompt block ordered by specificity."""

    resolved = settings or get_settings(cwd)
    sections: list[str] = []

    for path in discover_instruction_files(cwd, resolved):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            logger.warning("Failed to read instruction file: %s", path)
            continue

        if text:
            sections.append(f"Instructions from: {path}\n{text}")

    if not sections:
        return ""

    return "\n".join(
        [
            "<project_instructions>",
            "Ordered from global to project to cwd; later instructions take precedence.",
            "",
            "\n\n".join(sections),
            "</project_instructions>",
        ]
    )


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: str
    source: str
    body: str


# ---------------------------------------------------------------------
# Skill discovery from SKILL.md
# ---------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str] | None:
    """Extract YAML frontmatter and the instruction body."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    closing_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_index = index
            break

    if closing_index is None:
        return None

    try:
        parsed = yaml.safe_load("\n".join(lines[1:closing_index]))
    except yaml.YAMLError:
        return None

    if not isinstance(parsed, dict):
        return None
    return parsed, "\n".join(lines[closing_index + 1 :]).strip()


def _parse_skill_md(path: Path, source: str, fallback_name: str | None = None) -> Skill | None:
    """Parse a SKILL.md file and return a Skill, or None if invalid."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        logger.warning("Failed to read skill file: %s", path)
        return None

    parsed = _parse_frontmatter(text)
    if not parsed:
        logger.debug("No valid frontmatter in %s", path)
        return None
    frontmatter, body = parsed

    name: str | None = None
    for candidate in (frontmatter.get("name"), fallback_name):
        if not isinstance(candidate, str):
            continue
        candidate = candidate.strip()
        if not candidate or len(candidate) > _NAME_MAX_LEN or not _NAME_RE.match(candidate):
            continue
        name = candidate
        break

    if not name:
        logger.warning("Skill missing valid name: %s", path)
        return None

    raw_description = frontmatter.get("description")
    if not isinstance(raw_description, str) or not raw_description.strip():
        logger.warning("Skill missing description: %s (name=%s)", path, name)
        return None
    description = raw_description.strip()
    if len(description) > _DESCRIPTION_MAX_LEN:
        # Over the Agent Skills spec limit: load anyway so the skill stays
        # usable, but flag the bloat to the author.
        logger.warning("Skill description exceeds %d chars (%d): %s", _DESCRIPTION_MAX_LEN, len(description), path)

    return Skill(
        name=name,
        description=description,
        path=str(path.resolve()),
        source=source,
        body=body,
    )


def _scan_skill_root(root: Path, source: str) -> list[Skill]:
    """Scan one skills root for direct markdown skills and nested SKILL.md files."""

    if not root.is_dir():
        return []

    skills: list[Skill] = []
    seen_paths: set[str] = set()

    try:
        root_entries = [entry for entry in sorted(root.iterdir()) if not entry.name.startswith(".")]
    except PermissionError:
        logger.warning("Permission denied scanning: %s", root)
        return []

    for entry in root_entries:
        if not entry.is_file() or entry.suffix != ".md":
            continue
        real_path = str(entry.resolve())
        if real_path in seen_paths:
            continue
        seen_paths.add(real_path)

        skill = _parse_skill_md(entry, source, fallback_name=entry.stem)
        if skill:
            skills.append(skill)

    dirs_scanned = 0
    pending_dirs = deque((entry, 1) for entry in root_entries if entry.name not in _SKIP_DIRS and entry.is_dir())

    while pending_dirs and dirs_scanned < _MAX_DIRS_PER_ROOT:
        current, depth = pending_dirs.popleft()
        dirs_scanned += 1

        skill_md = current / "SKILL.md"
        if skill_md.is_file():
            real_path = str(skill_md.resolve())
            if real_path not in seen_paths:
                seen_paths.add(real_path)
                skill = _parse_skill_md(skill_md, source, fallback_name=current.name)
                if skill:
                    skills.append(skill)

        if current.is_symlink() or depth >= _MAX_SCAN_DEPTH:
            continue

        try:
            child_entries = [
                entry
                for entry in sorted(current.iterdir())
                if not entry.name.startswith(".") and entry.name not in _SKIP_DIRS
            ]
        except PermissionError:
            continue

        for entry in child_entries:
            if entry.is_dir():
                pending_dirs.append((entry, depth + 1))

    return skills


def discover_skills(cwd: str) -> list[Skill]:
    """Discover skills from global and project config roots."""

    home = Path.home()
    mycode_home = resolve_mycode_home()

    # Later roots win, so native mycode paths override compat paths and
    # nearer project config overrides global and parent project config.
    roots: list[tuple[Path, str]] = [
        (home / ".agents" / "skills", "global"),
        (mycode_home / "skills", "global"),
    ]
    for directory in project_dirs(cwd):
        roots.append((directory / ".agents" / "skills", "project"))
        roots.append((directory / ".mycode" / "skills", "project"))

    skills_by_name: dict[str, Skill] = {}
    seen_paths: set[str] = set()

    for root, source in roots:
        for skill in _scan_skill_root(root, source):
            if skill.path in seen_paths:
                continue
            seen_paths.add(skill.path)
            if skill.name in skills_by_name:
                previous = skills_by_name[skill.name]
                logger.debug(
                    "Skill %r from %s overrides %s (%s)", skill.name, skill.path, previous.path, previous.source
                )
            skills_by_name[skill.name] = skill

    return sorted(skills_by_name.values(), key=lambda skill: skill.name)


def discover_slash_skills(cwd: str) -> list[Skill]:
    """Return skills whose names do not collide with built-in slash commands."""

    return [skill for skill in discover_skills(cwd) if skill.name not in _RESERVED_SLASH_NAMES]


def build_skill_snapshot_blocks(text: str, cwd: str) -> list[ContentBlock]:
    """Expand standalone ``/<skill-name>`` references in first-use order."""

    skills = {skill.name: skill for skill in discover_slash_skills(cwd)}
    blocks: list[ContentBlock] = []
    seen: set[str] = set()
    for token in text.split():
        if not token.startswith("/"):
            continue
        name = token[1:]
        skill = skills.get(name)
        if not skill or name in seen:
            continue
        seen.add(name)
        base_dir = str(Path(skill.path).parent)
        snapshot = (
            f'<skill name="{skill.name}" location="{skill.path}">\nBase directory: {base_dir}\n\n{skill.body}\n</skill>'
        )
        blocks.append(text_block(snapshot, meta={"skill_snapshot": True}))
    return blocks


def load_skills_prompt(cwd: str) -> str:
    """Discover skills and format them as an <available_skills> block."""

    skills = discover_skills(cwd)
    if not skills:
        return ""
    logger.info("Discovered %d skill(s): %s", len(skills), ", ".join(skill.name for skill in skills))

    lines = [
        "When a task matches a skill's description, prefer the skill over manual alternatives. Read its <location> and follow the instructions.",
        "Relative paths inside a skill file resolve against the skill's directory (dirname of <location>).",
        "<available_skills>",
    ]
    # Escape element content so a description cannot break out of the block.
    for skill in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{html.escape(skill.name, quote=False)}</name>")
        lines.append(f"    <description>{html.escape(skill.description, quote=False)}</description>")
        lines.append(f"    <location>{html.escape(skill.path, quote=False)}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")

    prompt = "\n".join(lines)
    if len(prompt) > _SKILLS_PROMPT_WARN_CHARS:
        logger.warning(
            "Skills catalog is %d chars across %d skills; consider trimming descriptions", len(prompt), len(skills)
        )
    return prompt
