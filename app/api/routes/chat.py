import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.schemas.chat import ChatRequest, SessionCreateRequest
from app.services.session_service import SessionStore
from app.services.stream_service import stream_events

router = APIRouter()
store = SessionStore()


def set_api_key(model: str, api_key: str) -> None:
    """Set API key based on model prefix."""
    if model.startswith("anthropic:"):
        os.environ["ANTHROPIC_API_KEY"] = api_key
    elif model.startswith("openai:"):
        os.environ["OPENAI_API_KEY"] = api_key
    elif model.startswith("gemini:"):
        os.environ["GEMINI_API_KEY"] = api_key
    else:
        os.environ["OPENAI_API_KEY"] = api_key


def _parse_workspace_roots() -> list[Path]:
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
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    if not roots:
        roots.append(Path(os.getcwd()).resolve(strict=False))
    return roots


def _match_root(root_value: str) -> Path | None:
    requested = Path(root_value).expanduser().resolve(strict=False)
    for root in _parse_workspace_roots():
        if requested == root:
            return root
    return None


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


@router.post("/chat")
async def chat(req: ChatRequest):
    """SSE endpoint for chat."""
    settings = get_settings()
    model = req.model or settings.default_model or "anthropic:claude-sonnet-4-5"
    cwd = req.cwd or os.getcwd()
    api_base = req.api_base or settings.api_base

    if req.api_key:
        set_api_key(model, req.api_key)

    agent = await store.get_or_create(req.session_id, model=model, cwd=cwd, api_base=api_base)

    return StreamingResponse(
        stream_events(agent, req.message, on_done=lambda: store.save_session(req.session_id, agent)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/clear")
async def clear(session_id: str = "default"):
    """Clear conversation history."""
    await store.clear(session_id)
    return {"status": "ok"}


@router.post("/cancel")
async def cancel(session_id: str = "default"):
    """Cancel running tool processes for session."""
    agent = store.get(session_id)
    if agent:
        agent.cancel()
    return {"status": "ok"}


@router.post("/sessions")
async def create_session(req: SessionCreateRequest):
    """Create a new chat session."""
    settings = get_settings()
    model = req.model or settings.default_model or "anthropic:claude-sonnet-4-5"
    cwd = req.cwd or os.getcwd()
    api_base = req.api_base or settings.api_base
    return await store.create_session(req.title, model=model, cwd=cwd, api_base=api_base)


@router.get("/sessions")
async def list_sessions(cwd: str | None = None):
    """List chat sessions."""
    return {"sessions": await store.list_sessions(cwd=cwd)}


@router.get("/workspaces/roots")
async def list_workspace_roots():
    """List workspace roots for browsing."""
    roots = _parse_workspace_roots()
    return {"roots": [str(root) for root in roots]}


@router.get("/workspaces/browse")
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
        return {
            "root": str(root_path),
            "path": "",
            "current": str(root_path),
            "entries": [],
            "error": str(exc),
        }

    current_path = "" if target == root_path else target.relative_to(root_path).as_posix()
    return {
        "root": str(root_path),
        "path": current_path,
        "current": str(target),
        "entries": entries,
        "error": "",
    }


@router.get("/sessions/{session_id}")
async def load_session(session_id: str):
    """Load a chat session."""
    data = await store.load_session(session_id)
    if not data:
        return {"session": None, "messages": []}
    return data


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a chat session."""
    await store.delete(session_id)
    return {"status": "ok"}


@router.get("/config")
async def get_config():
    """Get current config."""
    settings = get_settings()
    return {
        "model": settings.default_model or "",
        "api_base": settings.api_base or "",
        "cwd": os.getcwd(),
    }


@router.get("/cwd")
async def list_cwd():
    """List current directory for UI validation."""
    return {
        "cwd": os.getcwd(),
        "exists": Path(os.getcwd()).exists(),
    }
