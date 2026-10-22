"""FastAPI application factory and ASGI entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from brain.ai.ask import LLMError
from brain.api import deps
from brain.api.routes import ask, capture, health, items, memory, ops, search
from brain.config import get_settings
from brain.services.items import ItemNotFound


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Close the shared AI clients when the app shuts down."""
    yield
    deps.close_shared_clients()


async def _instrument_requests(request: Request, call_next):
    """Record one api request span per non-ops request (method, path, ms).

    /ops and /cockpit paths are skipped so the dashboard's own polling does
    not flood the event log. Request/response bodies are never recorded.
    """
    import time

    from brain import ops as _ops

    path = request.url.path
    if path.startswith("/ops") or path.startswith("/cockpit"):
        return await call_next(request)
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        latency = (time.perf_counter() - started) * 1000
        is_ingest = request.method == "POST" and path.startswith(("/items", "/capture"))
        edge = "api->ingest" if is_ingest else None
        _ops.record_event(
            "api", "request", ok=status_code < 500, latency_ms=round(latency, 1),
            detail={"method": request.method, "path": path, "status": status_code},
            edge=edge,
        )


def create_app() -> FastAPI:
    """Build the FastAPI app with all routers and error handlers registered."""
    app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(items.router)
    app.include_router(capture.router)
    app.include_router(search.router)
    app.include_router(ask.router)
    app.include_router(ops.router)
    app.include_router(memory.router)
    app.middleware("http")(_instrument_requests)
    app.add_exception_handler(ItemNotFound, _handle_item_not_found)  # type: ignore[arg-type]
    app.add_exception_handler(LLMError, _handle_llm_unavailable)  # type: ignore[arg-type]
    return app


def _handle_item_not_found(request: Request, exc: ItemNotFound) -> JSONResponse:
    detail = f"memory item '{exc.item_id}' not found"
    return JSONResponse(status_code=404, content={"detail": detail})


def _handle_llm_unavailable(request: Request, exc: LLMError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": "LLM unavailable; try again later"})


app = create_app()
