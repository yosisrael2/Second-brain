"""FastAPI application factory and ASGI entrypoint."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from brain.api.routes import health, items
from brain.config import get_settings
from brain.services.items import ItemNotFound


def create_app() -> FastAPI:
    """Build the FastAPI app with all routers and error handlers registered."""
    app = FastAPI(title=get_settings().app_name)
    app.include_router(health.router)
    app.include_router(items.router)
    app.add_exception_handler(ItemNotFound, _handle_item_not_found)
    return app


def _handle_item_not_found(request: Request, exc: ItemNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": f"memory item '{exc.item_id}' not found"})


app = create_app()
