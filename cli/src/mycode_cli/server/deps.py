"""Shared dependencies for server routers."""

from __future__ import annotations

import os
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request

from mycode_cli.server.run_manager import RunManager
from mycode_cli.sessions import SessionStore


def resolve_workspace_cwd(raw: str | None) -> str:
    """Resolve a request cwd to an absolute path, rejecting a non-directory."""

    cwd = os.path.abspath(raw or os.getcwd())
    if not os.path.isdir(cwd):
        raise HTTPException(status_code=400, detail=f"Working directory does not exist: {cwd}")
    return cwd


async def get_store(request: Request) -> SessionStore:
    """Return this application's session store."""

    return cast(SessionStore, request.app.state.store)


async def get_run_manager(request: Request) -> RunManager:
    """Return this application's run manager."""

    return cast(RunManager, request.app.state.runs)


StoreDep = Annotated[SessionStore, Depends(get_store)]
RunManagerDep = Annotated[RunManager, Depends(get_run_manager)]
