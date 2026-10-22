"""Ops layer: events, heartbeats, spans, retention — framework-free.

Used by the API, the CLI and the Telegram bot. Every function swallows its
own DB failures (log + return): instrumentation must never break the
instrumented pipeline. Signatures are a published contract
(docs/ai-layer.md) — the Telegram bot calls these via its try-import shim:

- ``record_event(component, kind, ok, latency_ms=None, detail=None, edge=None)``
- ``heartbeat(component, detail=None)``
- ``with span(component, kind, edge=None) as s: ...``
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from brain.config import get_settings

logger = logging.getLogger(__name__)

RETENTION_DAYS = 7
_PRUNE_INTERVAL_S = 3600  # hourly retention sweep from the write path

_engine: Engine | None = None
_last_prune = 0.0


def _get_engine() -> Engine:
    """Memoized ops engine (rebuilt by :func:`reset_engine_for_tests`)."""
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def reset_engine_for_tests() -> None:
    """Drop the memoized engine (tests re-point DATABASE_URL)."""
    global _engine
    _engine = None


def record_event(
    component: str,
    kind: str,
    ok: bool,
    latency_ms: float | None = None,
    detail: dict[str, Any] | None = None,
    edge: str | None = None,
) -> None:
    """Record one ops event; NEVER raises into the caller."""
    try:
        with _get_engine().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ops_events (component, kind, ok, latency_ms, edge, detail) "
                    "VALUES (:component, :kind, :ok, :latency, :edge, "
                    "CAST(:detail AS jsonb))"
                ),
                {
                    "component": component[:32],
                    "kind": kind[:64],
                    "ok": ok,
                    "latency": latency_ms,
                    "edge": edge[:64] if edge else None,
                    "detail": json.dumps(_safe_detail(detail)),
                },
            )
        _maybe_prune()
    except Exception:  # noqa: BLE001  (ops must never break the caller)
        logger.debug("ops: record_event failed", exc_info=True)


def heartbeat(component: str, detail: dict[str, Any] | None = None) -> None:
    """Upsert the component's last-seen row; NEVER raises into the caller."""
    try:
        with _get_engine().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ops_heartbeats (component, last_seen, pid, version, detail) "
                    "VALUES (:c, now(), :pid, :version, CAST(:detail AS jsonb)) "
                    "ON CONFLICT (component) DO UPDATE SET "
                    "last_seen = excluded.last_seen, pid = excluded.pid, "
                    "version = excluded.version, detail = excluded.detail"
                ),
                {
                    "c": component[:32],
                    "pid": os.getpid(),
                    "version": git_sha(),
                    "detail": json.dumps(_safe_detail(detail)),
                },
            )
    except Exception:  # noqa: BLE001
        logger.debug("ops: heartbeat failed", exc_info=True)


class Span:
    """Handle yielded by :func:`span` — set ``detail``/``ok`` inside the block."""

    def __init__(self, component: str, kind: str, edge: str | None) -> None:
        self.component = component
        self.kind = kind
        self.edge = edge
        self.ok = True
        self.detail: dict[str, Any] = {}
        self._started = time.perf_counter()


@contextmanager
def span(component: str, kind: str, edge: str | None = None) -> Iterator[Span]:
    """Timing context manager: records latency + ok/exception type on exit.

    Usage::

        with ops.span("jev", "classify", edge="ingest->jev") as s:
            s.detail = {"labels": [...]}
    An exception marks ok=False with ``detail["error"] = "Type: msg"`` and
    re-raises — the caller's error handling is untouched.
    """
    handle = Span(component, kind, edge)
    try:
        yield handle
    except Exception as exc:  # noqa: BLE001  (record, then re-raise)
        handle.ok = False
        handle.detail = {**handle.detail, "error": f"{type(exc).__name__}: {exc!s}"[:200]}
        _record_span(handle)
        raise
    _record_span(handle)


def _record_span(handle: Span) -> None:
    latency_ms = (time.perf_counter() - handle._started) * 1000
    record_event(
        handle.component, handle.kind, handle.ok,
        latency_ms=round(latency_ms, 3), detail=handle.detail, edge=handle.edge,
    )


def prune_events(max_age_days: int = RETENTION_DAYS) -> int:
    """Delete ops_events older than the retention window; returns the count."""
    try:
        with _get_engine().begin() as conn:
            result = conn.execute(
                text("DELETE FROM ops_events WHERE ts < now() - make_interval(days => :d)"),
                {"d": max_age_days},
            )
            return int(result.rowcount or 0)
    except Exception:  # noqa: BLE001
        logger.debug("ops: prune failed", exc_info=True)
        return 0


def _maybe_prune() -> None:
    """Hourly retention sweep triggered from the write path (no thread)."""
    global _last_prune
    now = time.monotonic()
    if now - _last_prune >= _PRUNE_INTERVAL_S:
        _last_prune = now
        prune_events()


def heartbeat_age_seconds(component: str) -> float | None:
    """Seconds since the component's last heartbeat (None: never seen)."""
    try:
        with _get_engine().connect() as conn:
            row = conn.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM (now() - last_seen)) "
                    "FROM ops_heartbeats WHERE component = :c"
                ),
                {"c": component[:32]},
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:  # noqa: BLE001
        return None


def latest_event(component: str, *, ok: bool | None = None) -> dict[str, Any] | None:
    """The component's most recent event (optionally filtered by ok)."""
    try:
        query = (
            "SELECT ts, kind, ok, latency_ms, detail FROM ops_events "
            "WHERE component = :c"
        )
        params: dict[str, Any] = {"c": component[:32]}
        if ok is not None:
            query += " AND ok = :ok"
            params["ok"] = ok
        query += " ORDER BY ts DESC LIMIT 1"
        with _get_engine().connect() as conn:
            row = conn.execute(text(query), params).mappings().first()
        return dict(row) if row else None
    except Exception:  # noqa: BLE001
        return None


def git_sha() -> str | None:
    """Best-effort short git sha (None outside a repo / when git is missing)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        return out.stdout.strip()[:12] or None
    except Exception:  # noqa: BLE001
        return None


def _safe_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Defensive detail: bounded keys/values, JSON-safe scalars only."""
    if not detail:
        return {}
    safe: dict[str, Any] = {}
    for key, value in list(detail.items())[:16]:
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)[:64]] = value
        elif isinstance(value, (list, tuple)):
            safe[str(key)[:64]] = [str(v)[:64] for v in value[:16]]
        else:
            safe[str(key)[:64]] = str(value)[:120]
    return safe




def component_status() -> list[tuple[str, bool, str]]:
    """Per-component ``(name, ok, summary)`` for consumers like the bot's /status.

    The Telegram bot shim (feature/telegram) imports this when ``brain.ops``
    exists; it must never raise. Components that are intentionally off
    (``disabled``) count as ok — they are configuration, not failures.
    """
    try:
        from brain.ops.health import run_health_checks  # noqa: PLC0415

        health = run_health_checks()
    except Exception:  # noqa: BLE001  (the shim contract: never raise)
        return []
    lines: list[tuple[str, bool, str]] = []
    for name, entry in health.get("components", {}).items():
        ok = entry.get("status") in {"ok", "disabled"}
        summary = entry.get("summary") or entry.get("status", "?")
        lines.append((name, ok, summary))
    return lines
