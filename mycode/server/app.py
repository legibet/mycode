"""FastAPI application entry point."""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from mycode.core.config import setup_logging
from mycode.server.routers import chat_router, sessions_router, workspaces_router

logger = logging.getLogger(__name__)


def frontend_static_path() -> Path:
    """Return the packaged frontend static directory."""

    return Path(__file__).resolve().parent / "static"


def create_app(*, serve_frontend: bool = True) -> FastAPI:
    """Create the FastAPI app."""
    setup_logging()
    application = FastAPI(title="mycode")

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount API routers
    application.include_router(chat_router, prefix="/api")
    application.include_router(sessions_router, prefix="/api")
    application.include_router(workspaces_router, prefix="/api")

    if not serve_frontend:
        logger.info("frontend disabled; starting in API-only mode")
        return application

    frontend_static = frontend_static_path()
    if frontend_static.is_dir():
        application.mount("/", StaticFiles(directory=str(frontend_static), html=True), name="frontend")
    else:
        logger.warning("frontend assets not found at %s; starting in API-only mode", frontend_static)

    return application


app = create_app()
