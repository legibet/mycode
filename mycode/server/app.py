"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from mycode.core.config import setup_logging
from mycode.core.models import initialize_models_dev
from mycode.server.routers import chat_router, sessions_router, workspaces_router

logger = logging.getLogger(__name__)


def frontend_static_path() -> Path:
    """Return the packaged frontend static directory."""

    return Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Refresh the models.dev catalog once at startup. Request handling only
    # reads the in-memory or on-disk cache after this point.
    initialize_models_dev()
    yield


def create_app(*, serve_frontend: bool = True) -> FastAPI:
    """Create the FastAPI app."""
    setup_logging()
    application = FastAPI(title="mycode", lifespan=lifespan)

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
