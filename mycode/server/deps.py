"""Shared dependencies for server routers."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from mycode.core.session import SessionStore


@lru_cache
def get_store() -> SessionStore:
    """Return the shared session store for server requests."""

    return SessionStore()


StoreDep = Annotated[SessionStore, Depends(get_store)]
