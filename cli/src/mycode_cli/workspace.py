"""Workspace context shared by the CLI's tools and permission hooks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mycode_cli.background import BackgroundJobs

if TYPE_CHECKING:
    # config imports this module, so the type is only needed for checking.
    from mycode_cli.config import WebConfig


@dataclass(frozen=True)
class CliDeps:
    """Application context injected into tools/hooks via ``Agent(deps=...)``."""

    cwd: Path
    # Scratch area for large tool outputs (bash spill files, webfetch dumps).
    tool_output_dir: Path
    # Providers for the webfetch and websearch tools.
    web: WebConfig
    # The session's background job registry; ``None`` when the host cannot deliver results.
    jobs: BackgroundJobs | None = None

    @classmethod
    def for_session(
        cls,
        *,
        cwd: str | Path,
        data_dir: Path,
        session_id: str,
        web: WebConfig,
        jobs: BackgroundJobs | None = None,
    ) -> CliDeps:
        """Build the deps for one session; tool output lands next to its JSONL."""

        return cls(cwd=Path(cwd), tool_output_dir=data_dir / session_id / "tool-output", web=web, jobs=jobs)


def resolve_cwd(path: str | Path) -> str:
    """Normalize a workspace directory: absolute, with ``~`` and symlinks resolved.

    Settings, the session catalog, and tool context all use this form, so the
    TUI and the web server agree on which sessions belong to a workspace.
    """

    return str(Path(path).expanduser().resolve(strict=False))


def resolve_path(path: str | Path, *, cwd: str | Path) -> Path:
    """Resolve ``path`` against ``cwd``; absolute and ``~`` paths pass through."""

    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    return p.resolve(strict=False)
