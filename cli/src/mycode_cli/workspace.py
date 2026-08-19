"""Workspace context shared by the CLI's tools and permission hooks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CliDeps:
    """Application context injected into tools/hooks via ``Agent(deps=...)``."""

    cwd: Path
    # Scratch area for large tool outputs (bash spill files, webfetch dumps).
    tool_output_dir: Path

    @classmethod
    def for_session(cls, *, cwd: str | Path, data_dir: Path, session_id: str) -> CliDeps:
        """Build the deps for one session; tool output lands next to its JSONL."""

        return cls(cwd=Path(cwd), tool_output_dir=data_dir / session_id / "tool-output")


def resolve_path(path: str | Path, *, cwd: str | Path) -> Path:
    """Resolve ``path`` against ``cwd``; absolute and ``~`` paths pass through."""

    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    return p.resolve(strict=False)
