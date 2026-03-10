"""Skill discovery, parsing, and prompt formatting.

Scans multiple skill roots for SKILL.md files, parses YAML frontmatter,
and produces an <available_skills> block for injection into the system prompt.
The model uses the existing `read` tool to load full skill content on demand.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config import find_workspace_root, resolve_mycode_home

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


def _find_project_root(cwd: str) -> Path | None:
    """Walk up from *cwd* looking for a .git directory."""
    current = Path(cwd).resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _parse_frontmatter(text: str) -> dict | None:
    """Extract YAML frontmatter between --- delimiters."""
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    try:
        return yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return None


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

    fm = _parse_frontmatter(text)
    if not fm or not isinstance(fm, dict):
        # No frontmatter — skip unless we can derive both name and description
        logger.debug("No valid frontmatter in %s", path)
        return None

    name = _validate_name(fm.get("name")) or _validate_name(fallback_name)
    if not name:
        logger.warning("Skill missing valid name: %s", path)
        return None

    description = fm.get("description")
    if not description or not isinstance(description, str):
        logger.warning("Skill missing description: %s (name=%s)", path, name)
        return None

    description = description.strip()[:_DESC_MAX_LEN]

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

    # Direct *.md files at root level
    try:
        for entry in sorted(root.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix == ".md":
                real = str(entry.resolve())
                if real in seen_paths:
                    continue
                seen_paths.add(real)
                skill = _parse_skill_md(entry, source, fallback_name=entry.stem)
                if skill:
                    skills.append(skill)
    except PermissionError:
        logger.warning("Permission denied scanning: %s", root)
        return skills

    # BFS for subdirectories containing SKILL.md
    dirs_scanned = 0
    queue: list[tuple[Path, int]] = []

    try:
        for entry in sorted(root.iterdir()):
            if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                continue
            if entry.is_dir() and not entry.is_symlink():
                queue.append((entry, 1))
    except PermissionError:
        pass

    while queue and dirs_scanned < _MAX_DIRS_PER_ROOT:
        current, depth = queue.pop(0)
        dirs_scanned += 1

        skill_md = current / "SKILL.md"
        if skill_md.is_file():
            real = str(skill_md.resolve())
            if real not in seen_paths:
                seen_paths.add(real)
                skill = _parse_skill_md(skill_md, source, fallback_name=current.name)
                if skill:
                    skills.append(skill)

        if depth < _MAX_SCAN_DEPTH:
            try:
                for entry in sorted(current.iterdir()):
                    if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                        continue
                    if entry.is_dir() and not entry.is_symlink():
                        queue.append((entry, depth + 1))
            except PermissionError:
                pass

    return skills


def discover_skills(cwd: str) -> list[Skill]:
    """Discover skills from multiple roots. Later roots override earlier for same name.

    Scan order (lowest to highest priority):
    1. ~/.agents/skills/  (global)
    2. ~/.mycode/skills/  (global)
    3. {project_root}/.agents/skills/  (project)
    4. {project_root}/.mycode/skills/  (project)
    """
    home = Path.home()
    mycode_home = resolve_mycode_home()
    workspace_root = find_workspace_root(cwd)

    roots: list[tuple[Path, str]] = [
        (home / ".agents" / "skills", "global"),
        (mycode_home / "skills", "global"),
        (workspace_root / ".agents" / "skills", "project"),
        (workspace_root / ".mycode" / "skills", "project"),
    ]

    # Scan all roots; later entries override earlier for same name
    skills_by_name: dict[str, Skill] = {}
    seen_realpaths: set[str] = set()

    for root, source in roots:
        for skill in _scan_skill_root(root, source):
            if skill.path in seen_realpaths:
                continue
            seen_realpaths.add(skill.path)

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
