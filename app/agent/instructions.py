"""Workspace instruction discovery for AGENTS.md files."""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import Settings, find_workspace_root, get_settings, resolve_mycode_home

logger = logging.getLogger(__name__)

_MAX_INSTRUCTION_BYTES = 32 * 1024


def discover_instruction_files(cwd: str, settings: Settings | None = None) -> list[Path]:
    """Discover standard AGENTS.md files from global scope to project scope."""

    resolved_cwd = settings.cwd if settings else cwd
    workspace_root = Path(settings.workspace_root) if settings else find_workspace_root(resolved_cwd)
    home = Path.home().resolve(strict=False)
    mycode_home = resolve_mycode_home()

    files: list[Path] = []

    global_candidate = mycode_home / "AGENTS.md"
    compat_candidate = home / ".agents" / "AGENTS.md"
    if global_candidate.is_file():
        files.append(global_candidate)
    elif compat_candidate.is_file():
        files.append(compat_candidate)

    project_candidate = workspace_root / "AGENTS.md"
    if project_candidate.is_file():
        files.append(project_candidate)

    return files


def load_instructions_prompt(cwd: str, settings: Settings | None = None) -> str:
    """Load AGENTS.md files into a prompt block ordered from global to project."""

    resolved = settings or get_settings(cwd)
    remaining = _MAX_INSTRUCTION_BYTES
    sections: list[str] = []

    for path in discover_instruction_files(cwd, resolved):
        if remaining <= 0:
            break
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception:
            logger.warning("Failed to read instruction file: %s", path)
            continue

        if not text:
            continue

        encoded = text.encode("utf-8")
        truncated = False
        if len(encoded) > remaining:
            encoded = encoded[:remaining]
            text = encoded.decode("utf-8", errors="ignore").rstrip()
            truncated = True

        if not text:
            continue

        sections.append(f"## {path}\n{text}")
        remaining -= len(encoded)

        if truncated:
            sections.append("[Truncated due to instruction size limit.]")
            break

    if not sections:
        return ""

    body = "\n\n".join(sections)
    return "\n".join(
        [
            "<workspace_instructions>",
            "Instructions are ordered from global to project. Later files are more specific.",
            "",
            body,
            "</workspace_instructions>",
        ]
    )
