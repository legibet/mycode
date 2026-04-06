"""System prompt construction.

This module owns the full runtime system prompt:

- base prompt text (inlined below as _BASE_PROMPT)
- workspace instructions from AGENTS.md
- available skills from SKILL.md files
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import yaml

from mycode.core.config import Settings, get_settings, resolve_mycode_home

logger = logging.getLogger(__name__)

_MAX_SCAN_DEPTH = 3
_MAX_DIRS_PER_ROOT = 200
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git"})
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_NAME_MAX_LEN = 64

_BASE_PROMPT = """\
You are mycode, an expert coding assistant.

You have four tools: read, write, edit, bash.

- Use bash for file operations and exploration like `ls`, `find`, `rg`, etc.
- Always set offset/limit when reading large files.
- Always read files before editing them.
- Use write only for new files or complete rewrites
- Your response should be concise and relevant.
- When available skills match the current task, prefer them over manual alternatives. To use a skill: read its `SKILL.md`, then follow the instructions inside.\
"""


# ---------------------------------------------------------------------
# Full system prompt assembly
# ---------------------------------------------------------------------


def build_system_prompt(cwd: str, settings: Settings | None = None) -> str:
    """Build the full runtime system prompt for the current workspace."""

    resolved_cwd = str(Path(cwd).resolve(strict=False))
    resolved_settings = settings or get_settings(resolved_cwd)

    parts = [_BASE_PROMPT]

    instructions_prompt = load_instructions_prompt(resolved_cwd, resolved_settings)
    if instructions_prompt:
        parts.append(instructions_prompt)

    skills_prompt = load_skills_prompt(resolved_cwd)
    if skills_prompt:
        parts.append(skills_prompt)

    parts.append(f"Current working directory: {resolved_cwd}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------
# Workspace instructions from AGENTS.md
# ---------------------------------------------------------------------


def discover_instruction_files(cwd: str, settings: Settings | None = None) -> list[Path]:
    """Discover standard AGENTS.md files from global scope to current cwd."""

    resolved_cwd = settings.cwd if settings else cwd
    local_dir = Path(resolved_cwd).expanduser().resolve(strict=False)
    home = Path.home().resolve(strict=False)
    mycode_home = resolve_mycode_home()
    files: list[Path] = []

    global_candidate = mycode_home / "AGENTS.md"
    compat_candidate = home / ".agents" / "AGENTS.md"
    if global_candidate.is_file():
        files.append(global_candidate)
    elif compat_candidate.is_file():
        files.append(compat_candidate)

    local_candidate = local_dir / "AGENTS.md"
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
        except Exception:
            logger.warning("Failed to read instruction file: %s", path)
            continue

        if text:
            sections.append(f"## {path}\n{text}")

    if not sections:
        return ""

    return "\n".join(
        [
            "<workspace_instructions>",
            "Instructions are ordered from global to current cwd. Later files are more specific.",
            "",
            "\n\n".join(sections),
            "</workspace_instructions>",
        ]
    )


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: str
    source: str


# ---------------------------------------------------------------------
# Skill discovery from SKILL.md
# ---------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict[str, object] | None:
    """Extract YAML frontmatter between --- delimiters."""

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

    return parsed if isinstance(parsed, dict) else None


def _parse_skill_md(path: Path, source: str, fallback_name: str | None = None) -> Skill | None:
    """Parse a SKILL.md file and return a Skill, or None if invalid."""

    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        logger.warning("Failed to read skill file: %s", path)
        return None

    frontmatter = _parse_frontmatter(text)
    if not frontmatter:
        logger.debug("No valid frontmatter in %s", path)
        return None

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

    return Skill(
        name=name,
        description=raw_description.strip(),
        path=str(path.resolve()),
        source=source,
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
    pending_dirs = deque(
        (entry, 1)
        for entry in root_entries
        if entry.name not in _SKIP_DIRS and entry.is_dir() and not entry.is_symlink()
    )

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

        if depth >= _MAX_SCAN_DEPTH:
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
            if entry.is_dir() and not entry.is_symlink():
                pending_dirs.append((entry, depth + 1))

    return skills


def discover_skills(cwd: str) -> list[Skill]:
    """Discover skills from global and current-cwd roots. Later roots override earlier ones."""

    home = Path.home()
    mycode_home = resolve_mycode_home()
    cwd_path = Path(cwd).expanduser().resolve(strict=False)

    # Later roots win. This lets native mycode paths override compat paths, and
    # current-cwd config override global config with the same skill name.
    roots: list[tuple[Path, str]] = [
        (home / ".agents" / "skills", "global"),
        (mycode_home / "skills", "global"),
        (cwd_path / ".agents" / "skills", "project"),
        (cwd_path / ".mycode" / "skills", "project"),
    ]

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


def format_skills_for_prompt(skills: list[Skill]) -> str:
    """Format discovered skills as an <available_skills> block."""

    if not skills:
        return ""

    lines = ["<available_skills>"]
    for skill in skills:
        lines.append(f"- name: {skill.name}")
        lines.append(f"  path: {skill.path}")
        lines.append(f"  description: {skill.description}")
        lines.append("")
    lines.append("</available_skills>")
    return "\n".join(lines)


def load_skills_prompt(cwd: str) -> str:
    """Discover skills and return the formatted prompt block."""

    skills = discover_skills(cwd)
    if skills:
        logger.info("Discovered %d skill(s): %s", len(skills), ", ".join(skill.name for skill in skills))
    return format_skills_for_prompt(skills)
