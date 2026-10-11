"""FastAPI application factory and ASGI entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from brain.ai.ask import LLMError
from brain.api.routes import ask, health, items, search
from brain.config import get_settings
from brain.services.items import ItemNotFound


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Close the shared AI clients when the app shuts down."""
    yield
    from brain.api import deps  # imported late to avoid a circular import

    deps.close_shared_clients()


def create_app() -> FastAPI:
    """Build the FastAPI app with all routers and error handlers registered."""
    app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(items.router)
    app.include_router(search.router)
    app.include_router(ask.router)
    app.add_exception_handler(ItemNotFound, _handle_item_not_found)  # type: ignore[arg-type]
    app.add_exception_handler(LLMError, _handle_llm_unavailable)  # type: ignore[arg-type]
    return app


def _handle_item_not_found(request: Request, exc: ItemNotFound) -> JSONResponse:
    detail = f"memory item '{exc.item_id}' not found"
    return JSONResponse(status_code=404, content={"detail": detail})


def _handle_llm_unavailable(request: Request, exc: LLMError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": "LLM unavailable; try again later"})


app = create_app()
