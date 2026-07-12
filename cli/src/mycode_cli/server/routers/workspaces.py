"""Workspace browsing API endpoints."""

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query

from mycode.attachments import detect_document_mime_type, detect_image_mime_type

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

# Cap on entries returned by /files so a huge directory can't flood the client.
_FILE_LIST_LIMIT = 100


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

    if not target.is_relative_to(root_path):
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


def _classify_entry(path: Path) -> str:
    """Coarse attachment kind by magic bytes / extension for the @ menu."""
    if detect_image_mime_type(path):
        return "image"
    if detect_document_mime_type(path):
        return "document"
    return "text"


@router.get("/files")
def list_workspace_files(
    cwd: Annotated[str, Query(min_length=1)],
    dir: Annotated[str, Query()] = "",
    prefix: Annotated[str, Query()] = "",
) -> dict[str, object]:
    """List files and directories under ``cwd/dir`` for @ attachment completion.

    Directories first, then files, both sorted by name. Dotfiles are included
    (`.env`, `.github` matter when coding). Entries are filtered by ``prefix``
    on the server, then capped at 100 with a ``truncated`` flag.
    """
    base = Path(cwd).expanduser().resolve(strict=False)
    if not base.is_dir():
        return {"entries": [], "truncated": False, "error": "cwd does not exist"}

    target = (base / dir).resolve(strict=False)
    # Reject traversal outside cwd after resolving symlinks.
    if target != base and not target.is_relative_to(base):
        return {"entries": [], "truncated": False, "error": "path outside workspace"}
    if not target.is_dir():
        return {"entries": [], "truncated": False, "error": "not a directory"}

    matches: list[tuple[Path, bool]] = []
    try:
        for entry in target.iterdir():
            if prefix and not entry.name.startswith(prefix):
                continue
            try:
                resolved = entry.resolve()
                if resolved != base and not resolved.is_relative_to(base):
                    continue
                is_dir = entry.is_dir()
            except OSError:
                continue
            matches.append((entry, is_dir))
    except OSError as exc:
        return {"entries": [], "truncated": False, "error": str(exc)}

    matches.sort(key=lambda item: (not item[1], item[0].name.lower()))
    truncated = len(matches) > _FILE_LIST_LIMIT
    entries = []
    for entry, is_dir in matches[:_FILE_LIST_LIMIT]:
        rel = entry.relative_to(base).as_posix()
        entries.append(
            {
                "name": entry.name,
                "path": f"{rel}/" if is_dir else rel,
                "kind": "directory" if is_dir else _classify_entry(entry),
            }
        )
    return {"entries": entries, "truncated": truncated, "error": ""}
