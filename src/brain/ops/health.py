"""Per-component health checks and on-demand probes.

Cheap checks run on GET /ops/health (cached 5s); expensive probes
(embedder/jev/llm) only via POST /ops/probe/{component} — never on
auto-refresh. Every adapter is injectable via :class:`HealthContext` so
tests can fake any component. Checks never raise: failures degrade to
status "down" with the error type in the details.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from brain import ops
from brain.config import get_settings, unwrap_secret

_CACHE_TTL_S = 5.0
_cache: dict[str, Any] = {}
_PROBE_EMBED_TEXT = "cockpit probe sentence"
_PROBE_CLASSIFY_TEXT = "Buy milk tomorrow at 9am"
_PROBE_LLM_PROMPT = "Reply with exactly: OK"


class HealthContext:
    """Injectable adapters: each returns that component's raw check dict."""

    def __init__(
        self,
        db: Callable[[], dict[str, Any]] | None = None,
        qdrant: Callable[[], dict[str, Any]] | None = None,
        embedder: Callable[[], dict[str, Any]] | None = None,
        jev: Callable[[], dict[str, Any]] | None = None,
        llm: Callable[[], dict[str, Any]] | None = None,
        telegram: Callable[[], dict[str, Any]] | None = None,
        docker: Callable[[], dict[str, Any]] | None = None,
        disk: Callable[[], dict[str, Any]] | None = None,
        background: Callable[[], dict[str, Any]] | None = None,
        whatsapp: Callable[[], dict[str, Any]] | None = None,
        email: Callable[[], dict[str, Any]] | None = None,
        attachments: Callable[[], dict[str, Any]] | None = None,
        enrichment: Callable[[], dict[str, Any]] | None = None,
        triage: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.db = db or _db_check
        self.qdrant = qdrant or _qdrant_check
        self.embedder = embedder or _embedder_check
        self.jev = jev or _jev_check
        self.llm = llm or _llm_check
        self.telegram = telegram or _telegram_check
        self.docker = docker or _docker_check
        self.disk = disk or _disk_check
        self.background = background or _background_check
        self.whatsapp = whatsapp or _whatsapp_check
        self.triage = triage or _triage_check
        self.email = email or _email_check
        self.attachments = attachments or _attachments_check
        self.enrichment = enrichment or _enrichment_check


def reset_cache() -> None:
    """Drop the health cache (tests; new deploy)."""
    _cache.clear()


def _default_context() -> HealthContext:
    return HealthContext()


def _iso(value: Any) -> str:
    """Datetime -> ISO string (naive fallback for non-datetime values)."""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _entry(status: str, summary: str, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary,
        "checked_at": datetime.now(UTC).isoformat(),
        "latency_ms": details.get("latency_ms"),
        "details": details,
    }


def _derive_status(component: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Map an adapter dict to {status, summary} per the spec's rules."""
    if component == "api":
        return _entry("ok", f"pid {raw.get('pid')} · up {raw.get('uptime_s', 0):.0f}s", raw)
    if component == "postgres":
        if not raw.get("reachable", False):
            return _entry("down", "postgres unreachable", raw)
        if raw.get("drift"):
            return _entry("degraded", "alembic drift — run migrations", raw)
        items = raw.get("items_by_status") or {}
        return _entry("ok", f"{sum(items.values())} items · {raw.get('size', '?')}", raw)
    if component == "qdrant":
        if not raw.get("reachable", False):
            return _entry("down", "qdrant unreachable", raw)
        if not raw.get("collection_exists", False):
            return _entry("degraded", "collection missing — run brain reindex", raw)
        vectors, items = raw.get("vectors"), raw.get("items")
        if isinstance(vectors, int) and isinstance(items, int) and vectors != items:
            return _entry("degraded", f"vectors {vectors}/{items} — run brain reindex", raw)
        if not raw.get("dims_ok", True):
            return _entry("degraded", "embedder dim != collection dim", raw)
        return _entry("ok", f"vectors {vectors}/{items}", raw)
    if component == "embedder":
        if not raw.get("provider"):
            return _entry("disabled", "no embedder provider", raw)
        proven = raw.get("loaded", False) or (
            ops.recent_event("ingest", edge="ingest->embedder") is not None
            or ops.recent_event(edge="search->embedder") is not None
        )
        if not proven:
            return _entry(
                "not probed",
                f"{raw['provider']} configured — not proven yet",
                raw,
            )
        suffix = f" · {raw['model']}" if raw.get("model") else ""
        return _entry("ok", f"{raw['provider']}{suffix}", raw)
    if component == "jev":
        if not raw.get("configured", False):
            return _entry("disabled", "JEV_API_KEY unset — classify/rerank off", raw)
        return _entry("ok", f"key set · {raw.get('model', '?')}", raw)
    if component == "llm":
        provider = raw.get("provider", "?")
        activity = ops.recent_event("llm") or ops.recent_event(edge="ask->llm")
        if activity is not None and activity.get("ok") is False:
            return _entry("degraded", f"{provider} — last call failed", raw)
        if activity is not None:
            return _entry("ok", f"{provider} · answered {activity['kind']}", raw)
        return _entry("not probed", f"{provider} configured — run a probe to verify", raw)
    if component == "telegram":
        if not raw.get("token_set", False):
            return _entry("disabled", "TELEGRAM_BOT_TOKEN unset", raw)
        if not raw.get("allowlist_set", False):
            return _entry("degraded", "TELEGRAM_ALLOWED_USER_IDS unset", raw)
        age = raw.get("heartbeat_age_s")
        if age is None:
            return _entry("down", "bot never heartbeated", raw)
        if age > 1800:
            return _entry("down", f"heartbeat {age:.0f}s ago", raw)
        degraded_after = raw.get("degraded_after_s", 60)
        if age > degraded_after:
            return _entry("degraded", f"heartbeat {age:.0f}s ago", raw)
        return _entry("ok", f"heartbeat {age:.0f}s ago", raw)
    if component == "whatsapp":
        interval = raw.get("interval_s") or 30.0
        if not isinstance(interval, (int, float)) or interval <= 0:
            interval = 30.0
        heartbeat_age = raw.get("heartbeat_age_s")
        if heartbeat_age is None:
            return _entry("disabled", "poller has never heartbeated", raw)
        if heartbeat_age > 3 * interval:
            return _entry("degraded", f"heartbeat {heartbeat_age:.0f}s ago", raw)
        last_poll_age = raw.get("last_poll_age_s")
        channels = raw.get("channels")
        suffix = f" · {channels} channel(s)" if isinstance(channels, int) else ""
        if last_poll_age is None:
            return _entry("ok", f"heartbeat {heartbeat_age:.0f}s ago{suffix}", raw)
        return _entry("ok", f"polled {last_poll_age:.0f}s ago{suffix}", raw)
    if component == "email":
        if not raw.get("label_set", False):
            return _entry("disabled", "GMAIL_LABEL_ID unset", raw)
        if not raw.get("token_set", False):
            return _entry("disabled", "no refresh token — run brain gmail-auth", raw)
        state = raw.get("state")
        if state == "needs re-auth":
            return _entry("degraded", "needs re-auth", raw)
        if state == "label missing":
            return _entry("degraded", "label missing", raw)
        age = raw.get("heartbeat_age_s")
        if age is None:
            return _entry("down", "poller never heartbeated", raw)
        if age > 1800:
            return _entry("down", f"heartbeat {age:.0f}s ago", raw)
        if age > 150:
            return _entry("degraded", f"heartbeat {age:.0f}s ago", raw)
        return _entry("ok", f"poll ok · heartbeat {age:.0f}s ago", raw)
    if component == "attachments":
        last_error = raw.get("last_error")
        if last_error:
            return _entry(
                "degraded",
                f"queue {raw.get('queue_depth', 0)} · last error {last_error.get('kind')}",
                raw,
            )
        return _entry(
            "ok",
            f"queue {raw.get('queue_depth', 0)} · read today {raw.get('read_today', 0)}"
            f"/{raw.get('daily_budget', '?')}",
            raw,
        )
    if component == "enrichment":
        worker = raw.get("worker_enabled")
        summary = (
            f"queue {raw.get('queue_depth', 0)} · cap {raw.get('cap_used_today', 0)}"
            f"/{raw.get('cap_limit', '?')} · worker {'on' if worker else 'off (inert)'}"
            f" · extractor {raw.get('extractor', '?')}"
        )
        return _entry("ok", summary, raw)
    if component == "triage":
        return _entry("ok", raw.get("summary", "no decisions today"), raw)
    if component == "docker":
        if not raw.get("available", False):
            return _entry("disabled", "docker unavailable", raw)
        services = raw.get("services") or {}
        unhealthy = [
            name for name, state in services.items() if state not in {"healthy", "running"}
        ]
        if unhealthy:
            return _entry("degraded", f"unhealthy: {', '.join(unhealthy)}", raw)
        return _entry("ok", f"{len(services)} service(s)", raw)
    if component == "background":
        return _derive_background(raw)
    if component == "disk":
        free = raw.get("free_gb")
        if isinstance(free, (int, float)) and free < 5:
            return _entry("degraded", f"{free:.1f} GB free", raw)
        return _entry("ok", f"{free:.0f} GB free" if free is not None else "unknown", raw)
    return _entry("down", "unknown component", raw)


def run_health_checks(
    ctx: HealthContext | None = None, *, use_cache: bool = True
) -> dict[str, Any]:
    """Cheap checks for every component; cached 5s (never runs probes)."""
    if ctx is None and use_cache and _cache.get("fresh_at", 0) > time.monotonic() - _CACHE_TTL_S:
        return _cache["payload"]
    context = ctx or _default_context()
    raw = {
        "api": _safe(_api_check),
        "postgres": _safe(context.db),
        "qdrant": _safe(context.qdrant),
        "embedder": _safe(context.embedder),
        "jev": _safe(context.jev),
        "llm": _safe(context.llm),
        "telegram": _safe(context.telegram),
        "email": _safe(context.email),
        "attachments": _safe(context.attachments),
        "enrichment": _safe(context.enrichment),
        "triage": _safe(context.triage),
        "docker": _safe(context.docker),
        "background": _safe(context.background),
        "disk": _safe(context.disk),
        "whatsapp": _safe(context.whatsapp),
    }
    components = {name: _derive_status(name, data) for name, data in raw.items()}
    downs = [name for name, entry in components.items() if entry["status"] == "down"]
    degradeds = [name for name, entry in components.items() if entry["status"] == "degraded"]
    overall = "ok"
    if degradeds:
        overall = "degraded"
    if downs:
        overall = "down"
    payload = {
        "overall": overall,
        "degraded": sorted(set(downs) | set(degradeds)),
        "components": components,
        "checked_at": components["api"]["checked_at"],
    }
    if ctx is None and use_cache:
        _cache.clear()
        _cache["fresh_at"] = time.monotonic()
        _cache["payload"] = payload
    return payload


def _safe(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return dict(fn())
    except Exception as exc:  # noqa: BLE001  (a broken check is a down component)
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc!s}"[:200]}


# --- cheap component checks (default adapters) --------------------------------


def _api_check() -> dict[str, Any]:
    return {
        "latency_ms": 0.0,
        "uptime_s": round(time.monotonic() - _STARTED, 1),
        "version": ops.git_sha() or "?",
        "python": _python_version(),
        "host": "127.0.0.1",
        "port": int(_port()),
        "pid": __import__("os").getpid(),
    }


_STARTED = time.monotonic()


def _python_version() -> str:
    import sys  # noqa: PLC0415

    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _port() -> str:
    import os  # noqa: PLC0415

    return os.environ.get("PORT", "27800")


def _db_check() -> dict[str, Any]:
    from brain.db.session import engine  # noqa: PLC0415

    started = time.perf_counter()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        drift = _alembic_drift(conn)
        items_by_status = {
            row.status: row.count
            for row in conn.execute(
                text("SELECT status, count(*) AS count FROM memory_items GROUP BY status")
            ).mappings()
        }
        size = conn.execute(
            text("SELECT pg_size_pretty(pg_database_size(current_database()))")
        ).scalar_one()
    latency = (time.perf_counter() - started) * 1000
    return {
        "reachable": True,
        "latency_ms": round(latency, 1),
        "drift": drift,
        "items_by_status": items_by_status,
        "size": size,
    }


def _alembic_drift(conn: Any) -> bool:
    """True when the DB's alembic_version differs from the code's head."""
    from pathlib import Path  # noqa: PLC0415

    from alembic.config import Config  # noqa: PLC0415
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    repo_root = Path(__file__).resolve().parents[3]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "src/brain/db/migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
    current = row[0] if row else None
    return current != head


def _qdrant_check() -> dict[str, Any]:
    from qdrant_client import QdrantClient  # noqa: PLC0415

    settings = get_settings()
    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=unwrap_secret(settings.qdrant_api_key),
        timeout=settings.qdrant_timeout_s,
    )
    try:
        collections = {c.name for c in client.get_collections().collections}
        items = _item_count()
        if settings.qdrant_collection not in collections:
            return {"reachable": True, "collection_exists": False, "items": items}
        info = client.get_collection(settings.qdrant_collection)
        vectors = info.points_count or 0
        dims_ok = _dims_ok(info)
        return {
            "reachable": True,
            "collection_exists": True,
            "collection": settings.qdrant_collection,
            "vectors": vectors,
            "items": items,
            "dims_ok": dims_ok,
        }
    finally:
        client.close()


def _dims_ok(info: Any) -> bool:
    try:
        config = info.config.params.vectors
        dim = getattr(config, "size", None)
        if dim is None:
            return True
        expected = _embedder_dim()
        return expected is None or int(dim) == expected
    except Exception:  # noqa: BLE001
        return True  # unknown shape: don't fake a degradation


def _embedder_dim() -> int | None:
    """Embed one probe sentence — ONLY for the free local provider (no tokens)."""
    if get_settings().embedder_provider != "fastembed":
        return None  # keyed providers: skip (health checks must never cost tokens)
    try:
        from brain.api import deps  # noqa: PLC0415

        vector = deps.get_embedder().embed(_PROBE_EMBED_TEXT)
        return len(vector)
    except Exception:  # noqa: BLE001
        return None


def _item_count() -> int:
    from brain.db.session import engine  # noqa: PLC0415

    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM memory_items")).scalar_one())


def _embedder_check() -> dict[str, Any]:
    settings = get_settings()
    loaded = False
    try:
        from brain.api import deps  # noqa: PLC0415

        embedder = deps.get_embedder()
        # fastembed loads its model in __init__ (model resident = proven);
        # keyed providers are proven by traffic (checked in _derive_status).
        loaded = getattr(embedder, "_backend", None) is not None
    except Exception:  # noqa: BLE001
        pass
    return {
        "provider": settings.embedder_provider,
        "model": _embedder_model(settings),
        "loaded": loaded,
    }


def _embedder_model(settings: Any) -> str | None:
    if settings.embedder_provider == "openai":
        return settings.openai_embedding_model
    return settings.fastembed_model


def _jev_check() -> dict[str, Any]:
    settings = get_settings()
    configured = bool(unwrap_secret(settings.jev_api_key))
    return {"configured": configured, "model": settings.jev_model if configured else None}


def _llm_check() -> dict[str, Any]:
    settings = get_settings()
    model = (
        settings.anthropic_model
        if settings.llm_provider == "anthropic"
        else settings.claude_sdk_model
    )
    return {"provider": settings.llm_provider, "model": model or None}


# A healthy poll cycle spans one full long-poll window (poll_timeout_s) PLUS
# post-poll work (update batch processing, network overhead) — observed 89s
# against a timeout+30s threshold = 80s on 2026-10-24. The degraded threshold
# is therefore 2x the poll timeout: it scales with the configured timeout
# (one source of truth, no absolute constant) and leaves headroom over the
# worst observed healthy cycle; a truly stalled bot still crosses it and then
# the unchanged 1800s down rule. The timeout setting is unbounded, so the
# result is floored at 60s — with short polling (0) or a tiny timeout, 2x
# alone would make the card permanently degraded.
TELEGRAM_DEGRADED_FACTOR = 2
TELEGRAM_DEGRADED_FLOOR_S = 60


def _email_check() -> dict[str, Any]:
    """Raw email-poller facts: label + keychain token + last heartbeat state.

    The token is only probed for PRESENCE (never read, never hinted — the
    refresh token is a full credential, unlike the telegram token's last-4).
    """
    try:
        settings = get_settings()
    except Exception:  # noqa: BLE001  (config errors degrade the check)
        return {"label_set": False, "token_set": False, "heartbeat_age_s": None}
    label_set = bool(settings.gmail_label_id.strip())
    token_set = False
    if label_set:
        try:
            from brain.services.keychain import Keychain  # noqa: PLC0415

            token_set = Keychain().read("default") is not None
        except Exception:  # noqa: BLE001  (no keychain / denied: report, never raise)
            token_set = False
    age = ops.heartbeat_age_seconds("email")
    details: dict[str, Any] = {
        "label_set": label_set,
        "token_set": token_set,
        "heartbeat_age_s": age,
        "poll_interval_s": settings.gmail_poll_interval_s,
    }
    hb = ops.heartbeat_detail("email")
    if hb and hb.get("state"):
        details["state"] = hb["state"]
    elif label_set and token_set:
        details["state"] = "ok"
    last_error = ops.latest_event("email", ok=False)
    if last_error and not details.get("state") == "ok":
        details["last_error"] = {"kind": last_error["kind"], "ts": _iso(last_error["ts"])}
    return details


def _telegram_check() -> dict[str, Any]:
    try:
        settings = get_settings()
    except Exception:  # noqa: BLE001  (e.g. token set without allowlist)
        return {"token_set": True, "allowlist_set": False, "heartbeat_age_s": None}
    token_set = settings.telegram_bot_token is not None
    allowlist_set = bool(settings.telegram_allowed_user_ids.strip())
    age = ops.heartbeat_age_seconds("telegram")
    # The heartbeat threshold follows the bot's ACTUAL long-poll timeout: a
    # quiet getUpdates can occupy the whole window, so anything up to
    # timeout + margin is a healthy quiet poll, not a flap (ops polish).
    poll_timeout_s = int(settings.telegram_poll_timeout_s)
    details: dict[str, Any] = {
        "token_set": token_set,
        "allowlist_set": allowlist_set,
        "heartbeat_age_s": age,
        "poll_timeout_s": poll_timeout_s,
        "degraded_after_s": max(
            poll_timeout_s * TELEGRAM_DEGRADED_FACTOR, TELEGRAM_DEGRADED_FLOOR_S
        ),
    }
    last_error = ops.latest_event("telegram", ok=False)
    if last_error:
        details["last_error"] = {
            "kind": last_error["kind"],
            "ts": _iso(last_error["ts"]),
        }
    last_event = ops.latest_event("telegram")
    if last_event:
        details["last_update"] = {
            "kind": last_event["kind"],
            "ts": _iso(last_event["ts"]),
        }
    if token_set:
        token = unwrap_secret(settings.telegram_bot_token) or ""
        details["token_hint"] = f"…{token[-4:]}" if len(token) >= 4 else "set"
    return details


def _whatsapp_check() -> dict[str, Any]:
    """Poller health from the poller's OWN telemetry — never this process's env.

    The poller is configured by scripts/run-whatsapp.sh (the sanctioned live
    writer), which the API process cannot see; its heartbeat (ops) and the
    whatsapp_poll_state rows are the ground truth. Inferring 'configured'
    from this process's WHATSAPP_BRIDGE_DB reported 'disabled' for a RUNNING
    poller in the live cockpit (2026-10-23).
    """
    try:
        interval = get_settings().whatsapp_poll_interval_s
    except Exception:  # noqa: BLE001 — broken settings must not crash health
        interval = 30.0
    details: dict[str, Any] = {
        "interval_s": interval,
        "heartbeat_age_s": ops.heartbeat_age_seconds("whatsapp"),
    }
    try:
        from sqlalchemy import text  # noqa: PLC0415

        from brain.db.session import engine  # noqa: PLC0415

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT max(last_poll_at), count(*) FROM whatsapp_poll_state")
            ).one()
    except Exception:  # noqa: BLE001 — table missing/poll never ran: ages stay None
        return details
    last_poll_at, channels = row[0], int(row[1])
    details["channels"] = channels
    if last_poll_at is not None:
        details["last_poll_age_s"] = max(0.0, (datetime.now(UTC) - last_poll_at).total_seconds())
        details["last_poll_at"] = _iso(last_poll_at)
    return details


def _docker_check() -> dict[str, Any]:
    try:
        out = subprocess.run(
            ["docker", "compose", "ps", "--format", "json", "--all"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if out.returncode != 0:
            return {"available": False}
        services: dict[str, str] = {}
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            import json  # noqa: PLC0415

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = str(entry.get("Service") or entry.get("Name") or "?")
            health = entry.get("Health") or entry.get("State") or "unknown"
            services[name] = str(health)
        return {"available": True, "services": services, "project": _compose_project()}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}


def _compose_project() -> str | None:
    import os  # noqa: PLC0415

    return os.environ.get("COMPOSE_PROJECT_NAME")


# LaunchAgent labels managed by scripts/service.sh (the background services).
BACKGROUND_LABELS = (
    "com.secondbrain.api",
    "com.secondbrain.telegram",
    "com.secondbrain.email",
    "com.secondbrain.whatsapp",
)


def _background_check() -> dict[str, Any]:
    """Per-agent launchd status (launchctl print), never raises.

    available=False means launchd itself is unusable (no binary / timeout) —
    distinct from "agents simply not installed", where the agents map ends up
    empty and the cockpit shows the background node as disabled.
    """
    import os  # noqa: PLC0415

    agents: dict[str, dict[str, Any]] = {}
    for label in BACKGROUND_LABELS:
        try:
            out = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"available": False, "agents": {}}
        info: dict[str, Any] = {}
        if out.returncode == 0:
            # Only TOP-LEVEL (one-tab) keys: deeper 'state = active' lines live
            # in events/sockets sub-dicts and previously overwrote the job's
            # real state (a running agent read as 'active' => 'down').
            pattern = re.compile(r"^\t(state|pid|last exit code) = (.+)$")
            for line in out.stdout.splitlines():
                match = pattern.match(line)
                if match is None:
                    continue
                key, value = match.group(1), match.group(2).strip()
                if key == "state":
                    info.setdefault("state", value)
                elif key == "pid":
                    if value.isdigit():
                        info.setdefault("pid", int(value))
                elif key == "last exit code":
                    if value.lstrip("-").isdigit():
                        info.setdefault("last_exit", int(value))
        agents[label] = info
    return {"available": True, "agents": agents}


def _derive_background(raw: dict[str, Any]) -> dict[str, Any]:
    """Background-services node: launchd agents installed by service.sh.

    Not installed / launchd unusable -> disabled (dev via dev-start is a valid
    state, not an outage); installed and all running -> ok; partially running
    -> degraded; installed and none running -> down.
    """
    if not raw.get("available", False):
        return _entry("disabled", "launchd unavailable — background services not managed", raw)
    agents: dict[str, Any] = raw.get("agents") or {}
    installed = {label: info for label, info in agents.items() if info}
    if not installed:
        return _entry("disabled", "launchd agents not installed — scripts/service.sh install", raw)
    total = len(installed)
    running = [label for label, info in installed.items() if info.get("state") == "running"]
    stopped = [
        f"{label.split('.')[-1]}: {info.get('state', '?')}"
        + (f" (exit {info['last_exit']})" if info.get("last_exit") is not None else "")
        for label, info in installed.items()
        if info.get("state") != "running"
    ]
    if len(running) == total:
        return _entry("ok", f"{total}/{total} agents running", raw)
    if not running:
        return _entry("down", f"0/{total} agents running — {', '.join(stopped)}", raw)
    return _entry("degraded", f"{len(running)}/{total} agents running — {', '.join(stopped)}", raw)


def _disk_check() -> dict[str, Any]:
    usage = shutil.disk_usage("/")
    free_gb = usage.free / 1e9
    fastembed_free_gb = None
    from pathlib import Path  # noqa: PLC0415

    cache = Path.home() / ".cache" / "fastembed"
    if cache.exists():
        fastembed_free_gb = shutil.disk_usage(cache).free / 1e9
    return {
        "free_gb": round(free_gb, 1),
        "fastembed_free_gb": (
            round(fastembed_free_gb, 1) if fastembed_free_gb is not None else None
        ),
    }


# --- on-demand probes ---------------------------------------------------------


PROBE_COMPONENTS = {"postgres", "qdrant", "embedder", "jev", "llm", "telegram", "docker", "disk"}


def run_probe(component: str, ctx: HealthContext | None = None) -> dict[str, Any]:
    """Expensive probe for one component; on-demand only (POST /ops/probe)."""
    if component not in PROBE_COMPONENTS:
        return {"ok": False, "error": f"unknown component {component!r}"}
    started = time.perf_counter()
    try:
        if component == "embedder":
            result = _probe_embedder()
        elif component == "jev":
            result = _probe_jev()
        elif component == "llm":
            result = _probe_llm()
        else:
            context = ctx or _default_context()
            adapter = {
                "postgres": context.db,
                "qdrant": context.qdrant,
                "telegram": context.telegram,
                "docker": context.docker,
                "disk": context.disk,
            }[component]
            result = {"check": _safe(adapter)}
        latency = round((time.perf_counter() - started) * 1000, 1)
        # Record the probe so config-only components become "proven" (never raises).
        ops.record_event(
            component,
            "probe",
            bool(result.get("ok", True)),
            latency_ms=latency,
            detail={
                k: result[k]
                for k in ("summary", "dims", "model", "confidence")
                if result.get(k) is not None
            },
        )
        return {
            "component": component,
            "ok": result.get("ok", True),
            "latency_ms": latency,
            **result,
            "checked_at": _now(),
        }
    except Exception as exc:  # noqa: BLE001  (probes report failures, never raise)
        return {
            "component": component,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc!s}"[:200],
            "checked_at": _now(),
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _probe_embedder() -> dict[str, Any]:
    from brain.api import deps  # noqa: PLC0415

    started = time.perf_counter()
    vector = deps.get_embedder().embed(_PROBE_EMBED_TEXT)
    latency = round((time.perf_counter() - started) * 1000, 1)
    return {
        "ok": True,
        "dims": len(vector),
        "latency_ms": latency,
        "summary": f"embedded {len(vector)} dims",
    }


def _probe_jev() -> dict[str, Any]:
    from brain.api import deps  # noqa: PLC0415

    settings = get_settings()
    if not unwrap_secret(settings.jev_api_key):
        return {"ok": False, "error": "JEV_API_KEY unset"}
    classifier = deps.get_classifier()
    started = time.perf_counter()
    result = classifier.classify(_PROBE_CLASSIFY_TEXT)
    latency = round((time.perf_counter() - started) * 1000, 1)
    return {
        "ok": result.get("confidence", 0) > 0,
        "labels": {
            k: str(v) for k, v in result.items() if k in {"type", "importance", "actionability"}
        },
        "confidence": result.get("confidence"),
        "latency_ms": latency,
        "summary": f"confidence {result.get('confidence')}",
    }


def _probe_llm() -> dict[str, Any]:
    from brain.api import deps  # noqa: PLC0415

    started = time.perf_counter()
    completion = deps.get_llm_client().complete(_PROBE_LLM_PROMPT)
    latency = round((time.perf_counter() - started) * 1000, 1)
    ok = "OK" in completion.text
    return {
        "ok": ok,
        "model": completion.model,
        "latency_ms": latency,
        "summary": f"{completion.model or '?'} answered in {latency:.0f}ms",
    }


def _attachments_check() -> dict[str, Any]:
    """Attachment reading (F1): queue depth, budget usage, last error.

    Mirrors GET /ops/attachments (same in-process service — the API process
    owns the reading queue); read-only, never raises past _safe().
    """
    from brain.api.deps import _attachment_service  # noqa: PLC0415 (deferred: api cycle)

    svc = _attachment_service()
    details: dict[str, Any] = {
        "queue_depth": svc.queue_depth(),
        "read_today": svc.read_today(),
        "daily_budget": svc.daily_budget,
    }
    last_error = ops.latest_event("attachments", ok=False)
    if last_error:
        details["last_error"] = {"kind": last_error["kind"], "ts": _iso(last_error["ts"])}
    return details


def _triage_check() -> dict[str, Any]:
    """WhatsApp relevance triage (D2a): decisions recorded today by label.

    Reads today's ops_events rows (component='triage', kind='decided') —
    the same rows the triage service writes per decision. Counts only.

    'noise' counts only APPLIED decisions: the trier records 'decided'
    before the poller's keep-relevant guard rejects a downgrade, and every
    blocked pass also records exactly one 'downgrade_blocked' — so the
    blocked passes are decided(noise) minus downgrade_blocked (1:1).
    """
    from datetime import UTC, datetime, time  # noqa: PLC0415

    from sqlalchemy import text as sql_text  # noqa: PLC0415

    from brain.db.session import SessionLocal  # noqa: PLC0415

    day_start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    with SessionLocal() as session:
        rows = session.execute(
            sql_text(
                "SELECT detail->>'label' AS label, count(*) AS n FROM ops_events "
                "WHERE component = 'triage' AND kind = 'decided' AND ok "
                "AND ts >= :day GROUP BY label"
            ),
            {"day": day_start},
        ).fetchall()
        blocked = session.execute(
            sql_text(
                "SELECT count(*) FROM ops_events "
                "WHERE component = 'triage' AND kind = 'downgrade_blocked' "
                "AND ok AND ts >= :day"
            ),
            {"day": day_start},
        ).scalar_one()
    counts = {row.label: row.n for row in rows}
    relevant = counts.get("relevant", 0)
    # every blocked downgrade produced exactly one decided(noise) that was
    # NOT applied — subtract it (see docstring; 1:1 by construction)
    noise = max(counts.get("noise", 0) - blocked, 0)
    return {
        "relevant_today": relevant,
        "noise_today": noise,
        "summary": f"relevant {relevant} / noise {noise} today",
    }


def _enrichment_check() -> dict[str, Any]:
    """Extraction/enrichment (A1): queue depth, cap usage, worker on/off.

    Mirrors GET /ops/enrichment. The worker being OFF is the designed inert
    state (no children, no cards, no pings) — reported, never degraded.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    from brain.db.session import SessionLocal  # noqa: PLC0415
    from brain.repositories.items import ItemsRepository  # noqa: PLC0415
    from brain.services.extraction.service import EnrichmentService  # noqa: PLC0415

    now = datetime.now(tz=UTC)
    with SessionLocal() as session:
        service = EnrichmentService(ItemsRepository(session), session)
        details: dict[str, Any] = {
            "worker_enabled": get_settings().enrichment_worker_enabled,
            "extractor": get_settings().enrichment_extractor,
            "queue_depth": service.queue_depth(),
            "cap_used_today": service.cap_used_today(now),
            "cap_limit": get_settings().enrichment_daily_cap,
        }
    return details
