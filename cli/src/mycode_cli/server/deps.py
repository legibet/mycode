"""Shared dependencies for server routers."""

from __future__ import annotations

import os
from functools import cache
from typing import Annotated

from fastapi import Depends, HTTPException

from mycode_cli.config import resolve_sessions_dir
from mycode_cli.server.run_manager import RunManager
from mycode_cli.sessions import SessionStore


def resolve_workspace_cwd(raw: str | None) -> str:
    """Resolve a request cwd to an absolute path, rejecting a non-directory."""

    cwd = os.path.abspath(raw or os.getcwd())
    if not os.path.isdir(cwd):
        raise HTTPException(status_code=400, detail=f"Working directory does not exist: {cwd}")
    return cwd


@cache
def get_store() -> SessionStore:
    """Return the shared session store for server requests."""

    return SessionStore(data_dir=resolve_sessions_dir())


@cache
def get_run_manager() -> RunManager:
    """Return the shared in-process run manager."""

    return RunManager()


StoreDep = Annotated[SessionStore, Depends(get_store)]
RunManagerDep = Annotated[RunManager, Depends(get_run_manager)]
