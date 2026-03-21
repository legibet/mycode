"""Skill discovery, parsing, and prompt formatting.

Scans skill roots for SKILL.md files, parses YAML frontmatter, and produces an
<available_skills> block for injection into the system prompt. The model uses
the existing `read` tool to load full skill content on demand.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import yaml

from mycode.core.config import resolve_mycode_home

logger = logging.getLogger(__name__)

_MAX_SCAN_DEPTH = 3
_MAX_DIRS_PER_ROOT = 200
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git"})
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_NAME_MAX_LEN = 64
_DESC_MAX_LEN = 200


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: str  # absolute path to the SKILL.md file
    source: str  # "project" | "global"


def _parse_frontmatter(text: str) -> dict | None:
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


def _validate_name(name: str | None) -> str | None:
    """Return sanitized name or None if invalid."""
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    if len(name) > _NAME_MAX_LEN:
        return None
    if not _NAME_RE.match(name):
        return None
    return name


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

    name = _validate_name(frontmatter.get("name")) or _validate_name(fallback_name)
    if not name:
        logger.warning("Skill missing valid name: %s", path)
        return None

    raw_description = frontmatter.get("description")
    if not isinstance(raw_description, str) or not raw_description.strip():
        logger.warning("Skill missing description: %s (name=%s)", path, name)
        return None

    description = raw_description.strip()[:_DESC_MAX_LEN]

    return Skill(name=name, description=description, path=str(path.resolve()), source=source)


def _scan_skill_root(root: Path, source: str) -> list[Skill]:
    """Scan a single skills directory for SKILL.md files.

    Rules:
    - Direct *.md children of root are treated as skills (name from stem).
    - Subdirectories containing SKILL.md are treated as skills (name from frontmatter or dir name).
    - Max depth: _MAX_SCAN_DEPTH, max dirs: _MAX_DIRS_PER_ROOT.
    - Skip dotfiles/dotdirs, node_modules, __pycache__.
    """
    if not root.is_dir():
        return []

    skills: list[Skill] = []
    seen_paths: set[str] = set()
    root_entries: list[Path]

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
    """Discover skills from multiple roots. Later roots override earlier for same name.

    Scan order (lowest to highest priority):
    1. ~/.agents/skills/  (global)
    2. ~/.mycode/skills/  (global)
    3. {cwd}/.agents/skills/  (project)
    4. {cwd}/.mycode/skills/  (project)
    """
    home = Path.home()
    mycode_home = resolve_mycode_home()
    cwd_path = Path(cwd).expanduser().resolve(strict=False)

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
                prev = skills_by_name[skill.name]
                logger.debug(
                    "Skill %r from %s overrides %s (%s)",
                    skill.name,
                    skill.path,
                    prev.path,
                    prev.source,
                )
            skills_by_name[skill.name] = skill

    return sorted(skills_by_name.values(), key=lambda s: s.name)


def format_skills_for_prompt(skills: list[Skill]) -> str:
    """Format discovered skills as an <available_skills> block for the system prompt."""
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
    """Discover skills and return the formatted prompt block (empty if none found)."""
    skills = discover_skills(cwd)
    if skills:
        logger.info("Discovered %d skill(s): %s", len(skills), ", ".join(s.name for s in skills))
    return format_skills_for_prompt(skills)
