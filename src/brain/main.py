"""FastAPI application factory and ASGI entrypoint."""

from fastapi import FastAPI

from brain.api.routes import health
from brain.config import get_settings


def create_app() -> FastAPI:
    """Build the FastAPI app with all routers registered."""
    app = FastAPI(title=get_settings().app_name)
    app.include_router(health.router)
    return app


app = create_app()
