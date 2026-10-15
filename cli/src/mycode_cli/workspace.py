"""Workspace context shared by the CLI's tools and permission hooks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mycode_cli.background import BackgroundJobs
from mycode_cli.config import WebConfig


@dataclass(frozen=True)
class CliDeps:
    """Application context injected into tools/hooks via ``Agent(deps=...)``."""

    cwd: Path
    # Scratch area for large tool outputs (bash spill files, webfetch dumps):
    # ``SessionStore.tool_output_dir`` for the session.
    tool_output_dir: Path
    # Providers for the webfetch and websearch tools.
    web: WebConfig
    # The session's background job registry; ``None`` when the host cannot deliver results.
    jobs: BackgroundJobs | None = None


def resolve_path(path: str | Path, *, cwd: str | Path) -> Path:
    """Resolve ``path`` against ``cwd``; absolute and ``~`` paths pass through."""

    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    return p.resolve(strict=False)
