"""Ops + cockpit HTTP endpoints.

GET  /ops/health            cheap per-component checks (5s cache)
POST /ops/probe/{component} on-demand expensive probes (never on refresh)
POST /ops/trace/capture     dry-run: parse -> classify -> embed -> qdrant (no save)
GET  /ops/trace/search?q=   embedding -> candidates -> rerank order -> fallback
POST /ops/trace/ask         sources -> prompt sizes -> LLM -> answer+citations
GET  /ops/item/{id}         item inspector (row, vector presence, events)
GET  /ops/events            paginated event log (component/ok/since filters)
GET  /ops/stats             24h/7d stats + confidence histogram + status shares
GET  /ops/flow              flow-diagram nodes/edges with live status
GET  /cockpit/              the dashboard (static, vanilla JS, no CDN)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import text

from brain.config import get_settings, unwrap_secret
from brain.db.session import engine
from brain.ops import flow as ops_flow
from brain.ops import health as ops_health
from brain.ops import stats as ops_stats
from brain.ops import traces as ops_traces

router = APIRouter(tags=["ops"])

_COCKPIT_DIR = Path(__file__).resolve().parents[2] / "cockpit"


def _iso(value: Any) -> str:
    """Timestamp -> ISO string (datetimes from the DB rows)."""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


@router.get("/ops/health")
def get_ops_health() -> dict[str, Any]:
    """Cheap per-component health (cached 5s; expensive probes excluded)."""
    return ops_health.run_health_checks()


@router.post("/ops/probe/{component}")
def post_ops_probe(component: str) -> dict[str, Any]:
    """On-demand probe — the cockpit's "Run probe" button (llm probe costs tokens)."""
    if component not in ops_health.PROBE_COMPONENTS:
        raise HTTPException(status_code=404, detail=f"unknown component {component!r}")
    return ops_health.run_probe(component)


@router.get("/ops/flow")
def get_ops_flow() -> dict[str, Any]:
    """Flow-diagram payload: nodes/edges with live status + hop stats."""
    return ops_flow.flow_payload()


@router.post("/ops/trace/capture")
def post_trace_capture(payload: dict[str, Any]) -> dict[str, Any]:
    """Capture dry-run: parse -> classify -> embed -> neighbours; saves nothing."""
    body = str(payload.get("text") or "").strip()
    if not body:
        raise HTTPException(status_code=422, detail="text is required")
    return ops_traces.trace_capture(body)


@router.get("/ops/trace/search")
def get_trace_search(q: str) -> dict[str, Any]:
    return ops_traces.trace_search(q)


@router.post("/ops/trace/ask")
def post_trace_ask(payload: dict[str, Any]) -> dict[str, Any]:
    """Ask trace — runs the real LLM (user-initiated button, costs tokens)."""
    question = str(payload.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="question is required")
    return ops_traces.trace_ask(question)


@router.get("/ops/events")
def get_ops_events(
    component: str | None = None,
    ok: bool | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated ops events, newest first."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    base = "FROM ops_events"
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if component:
        conditions.append("component = :component")
        params["component"] = component
    if ok is not None:
        conditions.append("ok = :ok")
        params["ok"] = ok
    if since:
        conditions.append("ts > :since")
        params["since"] = since
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, ts, component, kind, ok, latency_ms, edge, detail "
                + base + where + " ORDER BY ts DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        ).mappings()
        events = []
        for row in rows:
            event = dict(row)
            event["id"] = str(event["id"])
            event["ts"] = _iso(event["ts"])
            events.append(event)
        total = int(conn.execute(text("SELECT count(*) " + base + where), params).scalar_one())
    return {"events": events, "total": total, "limit": limit, "offset": offset}


@router.get("/ops/item/{item_id}")
def get_item_inspector(item_id: str) -> dict[str, Any]:
    """Item inspector: DB row + vector presence + item events (no secrets)."""
    query = text(
        "SELECT id, raw_text, source, type, project, importance, actionability, "
        "confidence, status, occurred_at, meta, created_at, updated_at "
        "FROM memory_items WHERE id = CAST(:id AS uuid)"
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(query, {"id": item_id}).mappings().first()
    except Exception:
        raise HTTPException(status_code=404, detail=f"item {item_id} not found") from None
    if row is None:
        raise HTTPException(status_code=404, detail=f"item {item_id} not found")
    item = {key: _jsonable(row[key]) for key in row}
    item["id"] = str(row["id"])
    return {
        "item": item,
        "vector": _vector_for(item_id),
        "events": _item_events(item_id),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return value
    if hasattr(value, "value"):
        return value.value
    return value


def _vector_for(item_id: str) -> dict[str, Any]:
    """Vector presence + payload from qdrant (vectors themselves omitted — huge)."""
    from qdrant_client import QdrantClient  # noqa: PLC0415

    settings = get_settings()
    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=unwrap_secret(settings.qdrant_api_key),
        timeout=settings.qdrant_timeout_s,
    )
    try:
        points = client.retrieve(
            settings.qdrant_collection,
            ids=[item_id],
            with_payload=True,
            with_vectors=False,
        )
    except Exception as exc:  # noqa: BLE001  (qdrant down → inspector still works)
        return {"exists": False, "error": f"{type(exc).__name__}: {exc!s}"[:200]}
    finally:
        client.close()
    if not points:
        return {"exists": False}
    payload = dict(points[0].payload or {})
    return {
        "exists": True,
        "collection": settings.qdrant_collection,
        "payload": payload,
        "payload_keys": sorted(payload),
    }


def _item_events(item_id: str) -> list[dict[str, Any]]:
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, ts, component, kind, ok, latency_ms, edge, detail "
                    "FROM ops_events WHERE detail->>'item_id' = :id "
                    "ORDER BY ts DESC LIMIT 50"
                ),
                {"id": item_id},
            ).mappings()
    except Exception:  # noqa: BLE001
        return []
    events = []
    for row in rows:
        event = dict(row)
        event["id"] = str(event["id"])
        event["ts"] = _iso(event["ts"])
        events.append(event)
    return events


@router.get("/ops/stats")
def get_ops_stats() -> dict[str, Any]:
    """24h/7d per-component stats + confidence histogram + status shares."""
    return ops_stats.event_stats()


@router.get("/cockpit")
@router.get("/cockpit/")
def get_cockpit() -> FileResponse:
    """The dashboard shell — static HTML; app.js/app.css live beside it."""
    return FileResponse(_COCKPIT_DIR / "index.html", media_type="text/html")


@router.get("/cockpit/app.js")
def get_cockpit_js() -> FileResponse:
    return FileResponse(_COCKPIT_DIR / "app.js", media_type="application/javascript")


@router.get("/cockpit/app.css")
def get_cockpit_css() -> FileResponse:
    return FileResponse(_COCKPIT_DIR / "app.css", media_type="text/css")
