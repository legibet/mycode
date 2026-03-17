"""FastAPI application entry point."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from mycode.core.config import setup_logging
from mycode.server.routers import chat_router, sessions_router, workspaces_router


def create_app() -> FastAPI:
    """Create FastAPI application."""
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

    # Serve frontend static files if built
    frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if frontend_dist.exists():
        application.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return application


app = create_app()
