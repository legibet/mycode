"""Instruction discovery for AGENTS.md files."""

from __future__ import annotations

import logging
from pathlib import Path

from mycode.core.config import Settings, get_settings, resolve_mycode_home

logger = logging.getLogger(__name__)


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
    """Load AGENTS.md files into a prompt block ordered from global to current cwd."""

    resolved = settings or get_settings(cwd)
    sections: list[str] = []

    for path in discover_instruction_files(cwd, resolved):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception:
            logger.warning("Failed to read instruction file: %s", path)
            continue

        if not text:
            continue

        sections.append(f"## {path}\n{text}")

    if not sections:
        return ""

    body = "\n\n".join(sections)
    return "\n".join(
        [
            "<workspace_instructions>",
            "Instructions are ordered from global to current cwd. Later files are more specific.",
            "",
            body,
            "</workspace_instructions>",
        ]
    )
