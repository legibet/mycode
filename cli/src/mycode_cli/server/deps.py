"""Shared dependencies for server routers."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from mycode.session import SessionStore
from mycode_cli.config import resolve_sessions_dir
from mycode_cli.server.run_manager import RunManager


@lru_cache
def get_store() -> SessionStore:
    """Return the shared session store for server requests."""

    return SessionStore(data_dir=resolve_sessions_dir())


@lru_cache
def get_run_manager() -> RunManager:
    """Return the shared in-process run manager."""

    return RunManager()


StoreDep = Annotated[SessionStore, Depends(get_store)]
RunManagerDep = Annotated[RunManager, Depends(get_run_manager)]
