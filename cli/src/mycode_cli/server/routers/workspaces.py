"""Workspace browsing API endpoints."""

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _parse_workspace_roots() -> list[Path]:
    """Parse allowed workspace roots from environment variables."""
    raw = os.environ.get("MYCODE_WORKSPACE_ROOTS") or os.environ.get("WORKSPACE_ROOTS")
    if raw:
        candidates = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        candidates = [str(Path.home()), os.sep]

    roots: list[Path] = []
    seen: set[str] = set()
    for value in candidates:
        root = Path(value).expanduser().resolve(strict=False)
        if not root.exists():
            continue
        key = str(root)
        if key not in seen:
            seen.add(key)
            roots.append(root)

    return roots or [Path(os.getcwd()).resolve(strict=False)]


@router.get("/roots")
def list_workspace_roots() -> dict[str, list[str]]:
    """List workspace roots for browsing."""
    return {"roots": [str(root) for root in _parse_workspace_roots()]}


@router.get("/browse")
def browse_workspaces(
    root: Annotated[str, Query(min_length=1)],
    path: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    """Browse directories within a workspace root."""
    root_path = Path(root).expanduser().resolve(strict=False)
    if root_path not in _parse_workspace_roots():
        return {"root": root, "path": "", "current": "", "entries": [], "error": "Invalid root"}

    rel_path = Path(path) if path else Path()
    target = (root_path / rel_path).resolve(strict=False)

    try:
        target.relative_to(root_path)
    except ValueError:
        return {
            "root": str(root_path),
            "path": "",
            "current": str(root_path),
            "entries": [],
            "error": "Path outside root",
        }

    try:
        entries: list[dict[str, str]] = []
        for entry in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            try:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    entries.append({"name": entry.name, "path": entry.relative_to(root_path).as_posix()})
            except OSError:
                continue
    except OSError as exc:
        return {
            "root": str(root_path),
            "path": "",
            "current": str(root_path),
            "entries": [],
            "error": str(exc),
        }

    current_path = "" if target == root_path else target.relative_to(root_path).as_posix()
    return {"root": str(root_path), "path": current_path, "current": str(target), "entries": entries, "error": ""}


@router.get("/cwd")
def get_cwd() -> dict[str, object]:
    """Get current working directory."""
    cwd = os.getcwd()
    return {"cwd": cwd, "exists": Path(cwd).exists()}
