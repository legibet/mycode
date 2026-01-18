"""Workspace browsing API endpoints."""

import os
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _parse_workspace_roots() -> list[Path]:
    """Parse workspace roots from environment."""
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


def _match_root(root_value: str) -> Path | None:
    """Match requested root against allowed workspace roots."""
    requested = Path(root_value).expanduser().resolve(strict=False)
    for root in _parse_workspace_roots():
        if requested == root:
            return root
    return None


def _is_within(child: Path, parent: Path) -> bool:
    """Check if child path is within parent."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


@router.get("/roots")
async def list_workspace_roots():
    """List workspace roots for browsing."""
    return {"roots": [str(root) for root in _parse_workspace_roots()]}


@router.get("/browse")
async def browse_workspaces(root: str, path: str | None = None):
    """Browse directories within a workspace root."""
    root_path = _match_root(root)
    if not root_path:
        return {"root": root, "path": "", "current": "", "entries": [], "error": "Invalid root"}

    rel_path = Path(path) if path else Path()
    target = (root_path / rel_path).resolve(strict=False)

    if not _is_within(target, root_path):
        return {
            "root": str(root_path),
            "path": "",
            "current": str(root_path),
            "entries": [],
            "error": "Path outside root",
        }

    try:
        entries = []
        for entry in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            try:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    relative = entry.relative_to(root_path).as_posix()
                    entries.append({"name": entry.name, "path": relative})
            except OSError:
                continue
    except OSError as exc:
        return {"root": str(root_path), "path": "", "current": str(root_path), "entries": [], "error": str(exc)}

    current_path = "" if target == root_path else target.relative_to(root_path).as_posix()
    return {"root": str(root_path), "path": current_path, "current": str(target), "entries": entries, "error": ""}


@router.get("/cwd")
async def get_cwd():
    """Get current working directory."""
    return {"cwd": os.getcwd(), "exists": Path(os.getcwd()).exists()}
