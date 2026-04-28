"""FastAPI application entry point."""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from mycode_cli.config import setup_logging
from mycode_cli.server.routers import (
    chat_router,
    sessions_router,
    settings_router,
    workspaces_router,
)

logger = logging.getLogger(__name__)


def web_static_path() -> Path:
    """Return the packaged web static directory."""

    return Path(__file__).resolve().parent / "static"


def create_app(*, serve_web: bool = True) -> FastAPI:
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
    application.include_router(settings_router, prefix="/api")
    application.include_router(workspaces_router, prefix="/api")

    if not serve_web:
        logger.info("web UI disabled; starting in API-only mode")
        return application

    web_static = web_static_path()
    if web_static.is_dir():
        application.mount("/", StaticFiles(directory=str(web_static), html=True), name="web")
    else:
        logger.warning("web assets not found at %s; starting in API-only mode", web_static)

    return application


def create_web_app() -> FastAPI:
    """Create the app with packaged web assets."""
    return create_app(serve_web=True)


def create_api_app() -> FastAPI:
    """Create the API-only app."""
    return create_app(serve_web=False)


app = create_app()
