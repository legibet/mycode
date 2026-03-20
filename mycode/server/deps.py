"""Shared dependencies for server routers."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from mycode.core.session import SessionStore
from mycode.server.run_manager import RunManager


@lru_cache
def get_store() -> SessionStore:
    """Return the shared session store for server requests."""

    return SessionStore()


@lru_cache
def get_run_manager() -> RunManager:
    """Return the shared in-process run manager."""

    return RunManager()


StoreDep = Annotated[SessionStore, Depends(get_store)]
RunManagerDep = Annotated[RunManager, Depends(get_run_manager)]
