"""FastAPI application entry point."""

import logging
from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from mycode_cli.server.routers.chat import router as chat_router
from mycode_cli.server.routers.sessions import router as sessions_router
from mycode_cli.server.routers.settings import router as settings_router
from mycode_cli.server.routers.workspaces import router as workspaces_router

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
DEV_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def web_static_path() -> Path:
    """Return the packaged web static directory."""

    return Path(__file__).resolve().parent / "static"


def create_app(*, serve_web: bool = True, cors_origins: Sequence[str] = ()) -> FastAPI:
    """Create the FastAPI app."""
    application = FastAPI(title="mycode")

    if cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization"],
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
    return create_app(serve_web=False, cors_origins=DEV_CORS_ORIGINS)
