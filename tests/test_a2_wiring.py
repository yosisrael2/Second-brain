"""A2 wiring: the in-bot enrichment worker (default OFF).

When ENRICHMENT_WORKER_ENABLED=true the bot process runs one enrichment pass
per interval and delivers proposal cards through the SAME on_children_changed
hook the PR C tests use. When false (the default) nothing is scheduled and
nothing changes: no pass, no card, no DB write.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from brain.config import Settings
from brain.db.models import MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.services.extraction.service import EnrichmentService, RunReport
from brain.telegram_bot import BotServices, TelegramBot, _flush_bot_jobs
from conftest import assert_test_database

assert_test_database(str(engine.url))

SOURCE_BODY = "שלום,\nמחר כינוס הורים באמפי 22.10\nחולצה לבנה ליום חמישי 23.10\n"


def _flush(core, tg):
    async def go():
        await _flush_bot_jobs(tg, core)

    return asyncio.run(go())


def _fresh_bot_state(core) -> None:
    """Skip the ping branch so tests assert only on the enrichment tick."""
    import time

    core._last_ping_check = time.monotonic()  # noqa: SLF001 (same-module contract)


def _settings(**overrides):
    base: dict = dict(
        enrichment_worker_enabled=False,
        enrichment_worker_interval_s=300,
        _env_file=None,
    )
    base.update(overrides)
    return Settings(**base)


def _services(enrichment: Any) -> BotServices:
    return BotServices(ingest=None, items=None, search=None, ask=None, enrichment=enrichment)


def _bot(factory) -> TelegramBot:
    return TelegramBot(
        services_factory=factory,
        allowed_ids={7},
        timezone=ZoneInfo("UTC"),
    )


def _wipe(session) -> None:
    session.execute(text("DELETE FROM memory_items"))
    session.execute(text("DELETE FROM enrichment_jobs"))
    session.execute(text("DELETE FROM ops_events"))
    session.commit()


class _Tg:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, *, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text})


class _FakeEnrichment:
    """Stand-in: records runs, optionally raises, optionally fires the hook."""

    def __init__(self, *, source=None, error: Exception | None = None) -> None:
        self.calls: list[datetime] = []
        self.on_children_changed = None
        self._source = source
        self._error = error

    def run_once(self, now, *, limit=20, backfill=False):
        self.calls.append(now)
        if self._error is not None:
            raise self._error
        if self.on_children_changed is not None and self._source is not None:
            self.on_children_changed([SimpleNamespace(source=self._source, created=1, updated=0)])
        return RunReport(children_created=1)


# --- (a) the default: switch off -> nothing runs ------------------------------


def test_worker_disabled_schedules_nothing(monkeypatch):
    fake = _FakeEnrichment()

    @contextmanager
    def factory():
        yield _services(fake)

    monkeypatch.setattr("brain.config.get_settings", lambda: _settings())
    core = _bot(factory)
    _fresh_bot_state(core)
    _flush(core, _Tg())
    assert fake.calls == []  # zero passes
    assert core.drain_outbound() == []  # zero cards


# --- (b) switch on -> a pass runs and the card is delivered -------------------


def test_worker_enabled_runs_pass_and_delivers_card(monkeypatch):
    session = SessionLocal()
    _wipe(session)
    repo = ItemsRepository(session)
    source = repo.add(MemoryItem(raw_text=SOURCE_BODY, source="email"))
    repo.commit()
    service = EnrichmentService(repo, session)  # default (stub) extractor: deterministic
    service.enqueue(source)  # production state: capture already queued the source

    @contextmanager
    def factory():
        yield _services(service)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    tg = _Tg()
    core = _bot(factory)
    _fresh_bot_state(core)
    _flush(core, tg)

    # one pass ran (queue drained), the stub created children, and the
    # proposal card was DELIVERED to the allowed chat.
    jobs = session.execute(text("SELECT state FROM enrichment_jobs")).scalars().all()
    assert jobs and all(state == "done" for state in jobs)
    assert len(tg.sent) == 1
    assert "🧠 found" in tg.sent[0]["text"]
    _wipe(session)
    session.close()


# --- (c) a raising pass is logged + ops event; the bot keeps running ----------


def test_worker_pass_error_is_recorded_and_bot_survives(monkeypatch):
    events: list[dict] = []
    fake = _FakeEnrichment(error=RuntimeError("boom"))

    @contextmanager
    def factory():
        yield _services(fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    monkeypatch.setattr(
        "brain.telegram_bot.record_event",
        lambda component, name, ok=True, detail=None: events.append(
            {"component": component, "name": name, "ok": ok}
        ),
    )
    tg = _Tg()
    core = _bot(factory)
    _fresh_bot_state(core)
    _flush(core, tg)  # must not raise
    assert any(e["name"] == "worker_pass_failed" and e["ok"] is False for e in events)
    # the bot is alive: after the interval the next tick runs again
    core._enrich_last_run = 0.0
    _flush(core, tg)
    assert len(fake.calls) == 2


# --- (d) an overlapping tick is skipped ---------------------------------------


def test_overlapping_tick_is_skipped(monkeypatch):
    fake = _FakeEnrichment()

    @contextmanager
    def factory():
        yield _services(fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot(factory)
    _fresh_bot_state(core)
    core._enrich_running = True  # a previous pass is still in flight
    _flush(core, _Tg())
    assert fake.calls == []


# --- (e) the interval gates consecutive passes --------------------------------


def test_worker_pass_respects_the_interval(monkeypatch):
    fake = _FakeEnrichment()

    @contextmanager
    def factory():
        yield _services(fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot(factory)
    _fresh_bot_state(core)
    _flush(core, _Tg())
    assert len(fake.calls) == 1
    _flush(core, _Tg())  # inside the 300s interval -> skipped
    assert len(fake.calls) == 1
