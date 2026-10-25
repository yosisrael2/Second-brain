"""A2 wiring: the in-bot enrichment worker (default OFF).

When ENRICHMENT_WORKER_ENABLED=true the bot process runs one enrichment pass
per interval as a BACKGROUND task (the poll loop never waits for it) and
delivers proposal cards through the SAME on_children_changed hook the PR C
tests use — translated to cards on the loop thread, never from the worker.
The pass builds its own session + service (the per-update factory stays as
it was). When false (the default) nothing is scheduled and nothing changes.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from brain.config import Settings
from brain.db.models import ItemSource, MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.services.extraction.service import EnrichmentService, RunReport
from brain.telegram_bot import (
    BotServices,
    TelegramBot,
    _flush_bot_jobs,
    _shutdown_enrichment,
)
from conftest import assert_test_database

assert_test_database(str(engine.url))

SOURCE_BODY = "שלום,\nמחר כינוס הורים באמפי 22.10\nחולצה לבנה ליום חמישי 23.10\n"
# a WhatsApp text that qualifies for extraction (2+ dateish expressions)
WA_BODY = (
    "[21/10] הודעה מהכיתה: מחר 22/10 איסוף ב-13:00 "
    "וביום חמישי 23/10 חולצה לבנה וכך גם ב-24/10"
)


def _flush(core, tg):
    async def go():
        await _flush_bot_jobs(tg, core)

    return asyncio.run(go())


def _fresh_bot_state(core) -> None:
    """Skip the ping branch and clear any leftover pass task."""
    import time

    core._last_ping_check = time.monotonic()  # noqa: SLF001 (same module)
    core._enrich_task = None  # noqa: SLF001 (same module)


def _settings(**overrides):
    base: dict = dict(
        enrichment_worker_enabled=False,
        enrichment_worker_interval_s=300,
        _env_file=None,
    )
    base.update(overrides)
    return Settings(**base)


def _services() -> BotServices:
    return BotServices(ingest=None, items=None, search=None, ask=None)


_NOOP_FACTORY = _services  # the per-update factory: unused by the worker tests


def _bot() -> TelegramBot:
    @contextmanager
    def factory():
        yield _NOOP_FACTORY()

    return TelegramBot(
        services_factory=factory,
        allowed_ids={7},
        timezone=ZoneInfo("UTC"),
    )


def _use_pass_service(monkeypatch, service) -> None:
    """Swap the pass's service construction (the test seam; B3 keeps the
    per-update factory untouched)."""
    monkeypatch.setattr(
        "brain.telegram_bot._build_pass_service", lambda settings, session: service
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


class _BlockingEnrichment:
    """Fake service: fires the hook, then blocks until released (B1/B2)."""

    def __init__(self, outcomes: list | None = None) -> None:
        self.calls: list[Any] = []
        self.on_children_changed = None
        self._outcomes = outcomes or []
        self.started = threading.Event()
        self.release = threading.Event()
        self.done = False

    def run_once(self, now, *, limit=20, backfill=False, sources=None):
        self.calls.append(now)
        if self.on_children_changed is not None and self._outcomes:
            self.on_children_changed(list(self._outcomes))
        self.started.set()
        # the simulated LLM pass: slow; the poll loop must not wait for it
        if not self.release.wait(timeout=10):
            raise AssertionError("test pass was never released")
        self.done = True
        return RunReport(children_created=len(self._outcomes))


def _outcomes_for(*sources) -> list:
    return [SimpleNamespace(source=source, created=1, updated=0) for source in sources]


async def _await_pass_done(core, timeout: float = 5.0) -> None:
    async def wait():
        while core._enrich_task is None or not core._enrich_task.done():  # noqa: SLF001
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


async def _await_started(fake, timeout: float = 5.0) -> None:
    """The pass runs in a worker thread: give it a beat to actually enter."""
    async def wait():
        while not fake.started.is_set():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


# --- (a) the default: switch off -> nothing runs ------------------------------


def test_worker_disabled_schedules_nothing(monkeypatch):
    fake = _BlockingEnrichment()
    fake.release.set()
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr("brain.config.get_settings", lambda: _settings())
    core = _bot()
    _fresh_bot_state(core)
    _flush(core, _Tg())
    assert fake.calls == []  # zero passes
    assert core._enrich_task is None  # nothing scheduled
    assert core.drain_outbound() == []  # zero cards


# --- (B1) a running pass never blocks the poll loop ---------------------------


def test_worker_pass_does_not_block_the_flush(monkeypatch):
    fake = _BlockingEnrichment()
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        try:
            # the flush must RETURN while the pass is still running
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        except TimeoutError:
            fake.release.set()  # unblock the thread before the loop closes
            raise
        await _await_started(fake)  # the pass thread actually entered
        assert not fake.done  # ...but NOT finished
        # the poll loop stays responsive: a scripted update is processed
        # and answered while the pass is still in flight
        reply = core.route("/help", user_id=7)
        assert reply is not None and reply.text
        fake.release.set()
        await _await_pass_done(core)

    asyncio.run(go())
    assert fake.done
    assert len(fake.calls) == 1


# --- (B2) outcomes -> exactly one card per changed source, on the loop --------


def test_outcomes_become_exactly_one_card_per_changed_source(monkeypatch):
    session = SessionLocal()
    _wipe(session)
    repo = ItemsRepository(session)
    sources = []
    for body in (SOURCE_BODY, "פגישת הורים נוספת מחר בעשר"):
        source = repo.add(MemoryItem(raw_text=body, source="email"))
        sources.append(source)
    repo.commit()
    for source in sources:
        child = repo.add(
            MemoryItem(
                raw_text=f"child of {source.id}",
                source="email",
                child_state="proposed",
            )
        )
        repo.commit()
        session.add(
            ItemSource(child_id=child.id, source_id=source.id, quote=source.raw_text)
        )
        repo.commit()
    fake = _BlockingEnrichment(outcomes=_outcomes_for(*sources))
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        tg1 = _Tg()
        await asyncio.wait_for(_flush_bot_jobs(tg1, core), timeout=2.0)
        assert not fake.done  # pass in flight...
        # ...and core state was NOT touched from the worker thread: the
        # concurrent drain sees nothing (the cards come later, from the loop)
        assert core.drain_outbound() == []
        fake.release.set()
        await _await_pass_done(core)
        tg2 = _Tg()
        await _flush_bot_jobs(tg2, core)  # harvests the outcomes -> cards
        assert len(tg2.sent) == 2  # exactly one card per changed source
        # and they went out on the flush AFTER the pass, not mid-pass
        assert tg1.sent == []

    asyncio.run(go())
    _wipe(session)
    session.close()


# --- (B3) the per-update factory stays as it was ------------------------------


def test_update_factory_does_not_build_the_enrichment_service():
    from brain.telegram_bot import _live_services_factory

    src = inspect.getsource(_live_services_factory)
    # the enrichment service is built inside the pass only (once per pass,
    # never per update) — with the switch off the factory is untouched.
    assert "enrichment" not in src.lower()


# --- (c) a raising pass is logged + ops event; the bot keeps running ----------


def test_worker_pass_error_is_recorded_and_bot_survives(monkeypatch):
    events: list[dict] = []

    class _Raising:
        def __init__(self) -> None:
            self.on_children_changed = None
            self.calls = 0

        def run_once(self, now, *, limit=20, backfill=False, sources=None):
            self.calls += 1
            raise RuntimeError("boom")

    fake = _Raising()
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    monkeypatch.setattr(
        "brain.telegram_bot.record_event",
        lambda component, name, ok=True, detail=None: events.append(
            {"component": component, "name": name, "ok": ok, "detail": detail}
        ),
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_pass_done(core)
        # the harvest turns the task's exception into an ops event that
        # carries the error type + message
        await _flush_bot_jobs(_Tg(), core)
        failed = [e for e in events if e["name"] == "worker_pass_failed"]
        assert failed and failed[0]["ok"] is False
        assert "RuntimeError: boom" in failed[0]["detail"]["error"]
        # the bot is alive: after the interval the next tick runs again
        core._enrich_last_run = 0.0  # noqa: SLF001
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_pass_done(core)
        await _flush_bot_jobs(_Tg(), core)

    asyncio.run(go())
    assert fake.calls == 2


# --- (d) an overlapping tick is skipped ---------------------------------------


def test_overlapping_tick_is_skipped(monkeypatch):
    fake = _BlockingEnrichment()
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        try:
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
            await _await_started(fake)
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
            assert not fake.done  # still the SAME pass in flight
            assert len(fake.calls) == 1  # no second pass started
        finally:
            fake.release.set()
        await _await_pass_done(core)

    asyncio.run(go())


# --- (e) the interval gates consecutive passes (never the first) --------------


def test_worker_pass_respects_the_interval(monkeypatch):
    fake = _BlockingEnrichment()
    fake.release.set()  # passes finish instantly
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        # monotonic is boot-relative: _enrich_last_run == 0.0 means NEVER
        # ran, so the FIRST pass runs even on a freshly booted host.
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_pass_done(core)
        await _flush_bot_jobs(_Tg(), core)  # harvest + still inside 300s
        assert len(fake.calls) == 1  # no second pass
        core._enrich_last_run = 0.0  # noqa: SLF001  interval elapsed
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_pass_done(core)
        assert len(fake.calls) == 2

    asyncio.run(go())


# --- (b) switch on -> a pass runs and the card is delivered -------------------


def test_worker_enabled_runs_pass_and_delivers_card(monkeypatch):
    session = SessionLocal()
    _wipe(session)
    repo = ItemsRepository(session)
    source = repo.add(MemoryItem(raw_text=SOURCE_BODY, source="email"))
    repo.commit()
    service = EnrichmentService(repo, session)  # default (stub) extractor: deterministic
    service.enqueue(source)  # production state: capture already queued the source
    _use_pass_service(monkeypatch, service)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        tg1 = _Tg()
        await asyncio.wait_for(_flush_bot_jobs(tg1, core), timeout=2.0)
        assert tg1.sent == []  # the pass runs in the background
        await _await_pass_done(core)
        tg2 = _Tg()
        await _flush_bot_jobs(tg2, core)
        # one pass ran (queue drained), the stub created children, and the
        # proposal card was DELIVERED to the allowed chat.
        jobs = session.execute(text("SELECT state FROM enrichment_jobs")).scalars().all()
        assert jobs and all(state == "done" for state in jobs)
        assert len(tg2.sent) == 1
        assert "🧠 found" in tg2.sent[0]["text"]

    asyncio.run(go())
    _wipe(session)
    session.close()


# --- (shutdown) a finished-but-unharvested pass still delivers its card -------


def test_shutdown_delivers_a_finished_unharvested_pass(monkeypatch):
    session = SessionLocal()
    _wipe(session)
    repo = ItemsRepository(session)
    source = repo.add(MemoryItem(raw_text=SOURCE_BODY, source="email"))
    repo.commit()
    child = repo.add(
        MemoryItem(raw_text="child x", source="email", child_state="proposed")
    )
    repo.commit()
    session.add(ItemSource(child_id=child.id, source_id=source.id, quote=source.raw_text))
    repo.commit()
    fake = _BlockingEnrichment(outcomes=_outcomes_for(source))
    fake.release.set()  # the pass finishes on its own
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_pass_done(core)  # finished, but no flush harvested it yet
        tg = _Tg()
        await _shutdown_enrichment(tg, core)
        assert len(tg.sent) == 1  # the card went out during shutdown
        assert "🧠 found" in tg.sent[0]["text"]
        assert core._enrich_task is None  # noqa: SLF001  reference cleared

    asyncio.run(go())
    _wipe(session)
    session.close()


# --- (shutdown) a pass past the grace is cancelled and logged -----------------


def test_shutdown_cancels_a_pass_past_the_grace(monkeypatch):
    events: list[dict] = []
    fake = _BlockingEnrichment()  # blocks until released
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    monkeypatch.setattr(
        "brain.telegram_bot.record_event",
        lambda component, name, ok=True, detail=None: events.append(
            {"component": component, "name": name}
        ),
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        try:
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
            await _await_started(fake)
            # a pass past the grace: shutdown does not hang on it
            await asyncio.wait_for(
                _shutdown_enrichment(_Tg(), core, grace_s=0.05), timeout=2.0
            )
        finally:
            fake.release.set()
        assert any(
            e["name"] == "worker_pass_cancelled_at_shutdown" for e in events
        )
        assert core._enrich_task is None  # noqa: SLF001

    asyncio.run(go())


# --- (shutdown) no pass in flight: shutdown is a no-op ------------------------


def test_shutdown_without_a_pass_is_a_noop():
    core = _bot()

    async def go():
        await asyncio.wait_for(_shutdown_enrichment(_Tg(), core), timeout=2.0)

    asyncio.run(go())  # returns immediately, nothing raised
    assert core._enrich_task is None  # noqa: SLF001


# --- (deadline) a hung pass must not stall the worker forever -----------------


def _timeout_settings(**overrides):
    return _settings(enrichment_pass_timeout_s=0, **overrides)


def _wipe_deadline_state() -> None:
    """Deadline tests must not see earlier runs' ops rows (shared test DB)."""
    from brain.ops import _get_engine

    with _get_engine().begin() as conn:
        conn.execute(text("DELETE FROM ops_events WHERE component = 'enrichment'"))
        conn.execute(text("DELETE FROM ops_heartbeats WHERE component = 'enrichment-worker'"))


def _ops_events_of_kind(kind: str) -> list[dict]:
    from brain.ops import _get_engine

    with _get_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT component, kind, ok, detail FROM ops_events "
                "WHERE kind = :k ORDER BY ts"
            ),
            {"k": kind},
        ).mappings().all()
    return [dict(r) for r in rows]


def _health_enrichment_entry() -> dict:
    from brain.ops.health import _derive_status, _enrichment_check

    return _derive_status("enrichment", _enrichment_check())


def test_pass_past_deadline_records_timeout_and_degrades_health(monkeypatch):
    from brain.ops import heartbeat_detail

    _wipe_deadline_state()

    fake = _BlockingEnrichment()
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _timeout_settings(enrichment_worker_enabled=True)
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        try:
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
            await _await_started(fake)
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        finally:
            pass
        events = _ops_events_of_kind("worker_pass_timeout")
        assert len(events) == 1
        assert events[0]["ok"] is False
        assert events[0]["detail"]["extractor"] == "stub"
        hb = heartbeat_detail("enrichment-worker")
        assert hb is not None and hb["phase"] == "stalled"
        entry = _health_enrichment_entry()
        assert entry["status"] == "degraded"
        assert "pass running" in entry["summary"] and "> deadline" in entry["summary"]

    asyncio.run(go())
    fake.release.set()


def test_stalled_thread_finishing_lets_the_next_due_tick_run(monkeypatch):
    from brain.ops import heartbeat_detail

    _wipe_deadline_state()

    fake = _BlockingEnrichment()
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _timeout_settings(enrichment_worker_enabled=True)
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_started(fake)
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)  # timeout fires
        assert len(fake.calls) == 1
        fake.release.set()
        while core._enrich_stalled_task is not None and not core._enrich_stalled_task.done():  # noqa: SLF001
            await asyncio.sleep(0.01)
        core._enrich_last_run = 0.0  # noqa: SLF001  the next tick is due
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_pass_done(core)  # a NEW pass ran
        assert len(fake.calls) == 2
        hb = heartbeat_detail("enrichment-worker")
        assert hb["phase"] in ("running", "idle")  # recovered

    asyncio.run(go())


def test_no_second_concurrent_pass_while_the_stalled_thread_is_alive(monkeypatch):
    _wipe_deadline_state()
    fake = _BlockingEnrichment()
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _timeout_settings(enrichment_worker_enabled=True)
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        try:
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
            await _await_started(fake)
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)  # timeout
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)  # later tick
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)  # and again
        finally:
            fake.release.set()
        assert len(fake.calls) == 1  # still only the ORIGINAL pass
        assert len(_ops_events_of_kind("worker_pass_timeout")) == 1  # logged once

    asyncio.run(go())


def test_health_enrichment_ok_without_a_worker_heartbeat(monkeypatch):
    session = SessionLocal()
    session.execute(text("DELETE FROM ops_heartbeats WHERE component = 'enrichment-worker'"))
    session.commit()
    monkeypatch.setattr("brain.config.get_settings", lambda: _settings())
    entry = _health_enrichment_entry()
    assert entry["status"] == "ok"  # worker off (inert) stays the designed ok
    session.close()


# --- (deadline) a finished STALLED pass still delivers its cards ---------------


def test_finished_stalled_pass_still_delivers_its_cards(monkeypatch):
    session = SessionLocal()
    _wipe(session)
    _wipe_deadline_state()
    repo = ItemsRepository(session)
    source = repo.add(MemoryItem(raw_text=SOURCE_BODY, source="email"))
    repo.commit()
    child = repo.add(
        MemoryItem(raw_text="child x", source="email", child_state="proposed")
    )
    repo.commit()
    session.add(ItemSource(child_id=child.id, source_id=source.id, quote=source.raw_text))
    repo.commit()
    fake = _BlockingEnrichment(outcomes=_outcomes_for(source))
    _use_pass_service(monkeypatch, fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _timeout_settings(enrichment_worker_enabled=True)
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        try:
            tg1 = _Tg()
            await asyncio.wait_for(_flush_bot_jobs(tg1, core), timeout=2.0)
            await _await_started(fake)
            await asyncio.wait_for(_flush_bot_jobs(tg1, core), timeout=2.0)  # deadline
            assert tg1.sent == [] and len(_ops_events_of_kind("worker_pass_timeout")) == 1
            fake.release.set()
            while core._enrich_stalled_task is None or not core._enrich_stalled_task.done():  # noqa: SLF001
                await asyncio.sleep(0.01)
            tg2 = _Tg()
            await _flush_bot_jobs(tg2, core)  # the stalled task's harvest + flush
            # the pass ran past its deadline, but its changed source STILL
            # gets exactly one proposal card
            assert len(tg2.sent) == 1
            assert "🧠 found" in tg2.sent[0]["text"]
        finally:
            fake.release.set()

    asyncio.run(go())
    _wipe(session)
    session.close()


# --- (step 2, week 1) the worker's source filter ------------------------------


def _queued(session, *, source: str, text: str, sent_days_ago: int = 1):
    """A captured parent + its production-queued enrichment job."""
    repo = ItemsRepository(session)
    item = repo.add(
        MemoryItem(
            raw_text=text,
            source=source,
            occurred_at=datetime.now(tz=UTC) - timedelta(days=sent_days_ago),
        )
    )
    service = EnrichmentService(repo, session)
    service.enqueue(item)
    return item


def _job_row(session, item_id):
    return session.execute(
        text("SELECT attempts, not_before, state FROM enrichment_jobs WHERE item_id = :i"),
        {"i": str(item_id)},
    ).one()


def test_worker_source_filter_processes_only_email(monkeypatch):
    """Week 1: with ENRICHMENT_WORKER_SOURCES=email the worker pass runs the
    email job ONLY — the WhatsApp job stays queued and its row is UNTOUCHED
    (attempts / not_before / state identical)."""
    session = SessionLocal()
    _wipe(session)
    email = _queued(session, source="email", text=SOURCE_BODY)
    whatsapp = _queued(session, source="whatsapp", text=WA_BODY)
    snapshot = _job_row(session, whatsapp.id)
    service = EnrichmentService(ItemsRepository(session), session)
    _use_pass_service(monkeypatch, service)
    monkeypatch.setattr(
        "brain.config.get_settings",
        lambda: _settings(enrichment_worker_enabled=True, enrichment_worker_sources="email"),
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_pass_done(core)
        tg2 = _Tg()
        await _flush_bot_jobs(tg2, core)
        assert len(tg2.sent) == 1  # only the email's card

    asyncio.run(go())
    assert _job_row(session, email.id).state == "done"
    assert _job_row(session, whatsapp.id) == snapshot  # attempts/not_before/state identical
    _wipe(session)
    session.close()


def test_worker_without_filter_processes_all_sources(monkeypatch):
    """Default (empty ENRICHMENT_WORKER_SOURCES): every source drains."""
    session = SessionLocal()
    _wipe(session)
    _queued(session, source="email", text=SOURCE_BODY)
    _queued(session, source="whatsapp", text=WA_BODY)
    service = EnrichmentService(ItemsRepository(session), session)
    _use_pass_service(monkeypatch, service)
    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_pass_done(core)
        states = session.execute(text("SELECT state FROM enrichment_jobs")).scalars().all()
        assert states and all(state == "done" for state in states)

    asyncio.run(go())
    _wipe(session)
    session.close()


def test_cli_run_once_stays_unfiltered():
    """The filter is the WORKER's: a plain run_once (brain enrich) processes
    every source — backfills are never silently skipped."""
    session = SessionLocal()
    _wipe(session)
    _queued(session, source="email", text=SOURCE_BODY)
    _queued(session, source="whatsapp", text=WA_BODY)
    service = EnrichmentService(ItemsRepository(session), session)
    report = service.run_once(datetime.now(tz=UTC), limit=20)
    assert report.processed == 2, report
    _wipe(session)
    session.close()


def test_cap_counts_only_executed_jobs(monkeypatch):
    """Cap=1 with 2 email + 1 WhatsApp queued: one email runs, ONE email is
    deferred per D5, the WhatsApp job is untouched (never deferred)."""
    session = SessionLocal()
    _wipe(session)
    e1 = _queued(session, source="email", text=SOURCE_BODY, sent_days_ago=3)
    e2 = _queued(session, source="email", text=SOURCE_BODY + " א", sent_days_ago=2)
    wa = _queued(session, source="whatsapp", text=WA_BODY, sent_days_ago=1)
    # the pass seam bypasses _build_pass_service: the cap lives on the
    # service we inject (the real builder forwards settings.enrichment_daily_cap)
    service = EnrichmentService(ItemsRepository(session), session, daily_cap=1)
    _use_pass_service(monkeypatch, service)
    monkeypatch.setattr(
        "brain.config.get_settings",
        lambda: _settings(
            enrichment_worker_enabled=True,
            enrichment_worker_sources="email",
            enrichment_daily_cap=1,
        ),
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_pass_done(core)

    asyncio.run(go())
    rows = {
        str(j["item_id"]): j
        for j in session.execute(
            text("SELECT item_id, state, not_before FROM enrichment_jobs")
        ).mappings()
    }
    assert rows[str(e1.id)]["state"] == "done"
    assert rows[str(e2.id)]["state"] == "queued"  # deferred over cap (D5)
    assert rows[str(e2.id)]["not_before"] is not None  # pushed to tomorrow
    assert rows[str(wa.id)]["state"] == "queued"
    assert rows[str(wa.id)]["not_before"] is None  # never deferred, never touched
    _wipe(session)
    session.close()


def test_starvation_the_filter_is_in_sql(monkeypatch):
    """50 QUALIFYING old WhatsApp jobs ahead of ONE newer email job: the very
    first filtered pass runs the email (limit 25 could never reach it if the
    filter were a post-fetch skip)."""
    session = SessionLocal()
    _wipe(session)
    for _ in range(50):
        _queued(session, source="whatsapp", text=WA_BODY, sent_days_ago=10)
    email = _queued(session, source="email", text=SOURCE_BODY, sent_days_ago=1)
    # deterministic queue order: the fillers enqueued strictly earlier than
    # the email (same-second queued_at would fall back to random uuid order)
    session.execute(
        text("UPDATE enrichment_jobs SET queued_at = :q WHERE item_id <> :i"),
        {"q": datetime.now(tz=UTC) - timedelta(days=2), "i": str(email.id)},
    )
    session.commit()
    # the premise, asserted: 51 queued jobs, 50 WhatsApp, ALL older than the
    # email's job — if enqueue had skipped the fillers this would prove nothing
    counts = session.execute(
        text(
            "SELECT source, COUNT(*) FROM enrichment_jobs j "
            "JOIN memory_items m ON m.id = j.item_id "
            "WHERE j.state = 'queued' GROUP BY source"
        )
    ).all()
    assert dict(counts) == {"whatsapp": 50, "email": 1}, counts
    email_queued_at = session.execute(
        text("SELECT queued_at FROM enrichment_jobs WHERE item_id = :i"),
        {"i": str(email.id)},
    ).scalar()
    older = session.execute(
        text("SELECT COUNT(*) FROM enrichment_jobs WHERE item_id <> :i AND queued_at < :q"),
        {"i": str(email.id), "q": email_queued_at},
    ).scalar()
    assert older == 50  # every filler is strictly ahead of the email job
    service = EnrichmentService(ItemsRepository(session), session)
    _use_pass_service(monkeypatch, service)
    monkeypatch.setattr(
        "brain.config.get_settings",
        lambda: _settings(enrichment_worker_enabled=True, enrichment_worker_sources="email"),
    )
    core = _bot()
    _fresh_bot_state(core)

    async def go():
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_pass_done(core)
        state = session.execute(
            text("SELECT state FROM enrichment_jobs WHERE item_id = :i"),
            {"i": str(email.id)},
        ).scalar()
        assert state == "done"

    asyncio.run(go())
    _wipe(session)
    session.close()


def test_starvation_deferred_jobs_never_hide_a_due_job():
    """Same post-fetch starvation class, not_before leg: 30 jobs deferred to
    tomorrow (not_before) sit ahead of 1 DUE job in queue order. The 25-row
    scan window must not fill up with not-yet-due rows — the due job runs."""
    session = SessionLocal()
    _wipe(session)
    tomorrow = datetime.now(tz=UTC) + timedelta(days=1)
    for _ in range(30):
        _queued(session, source="email", text=SOURCE_BODY, sent_days_ago=10)
    due = _queued(session, source="email", text=SOURCE_BODY + " ד", sent_days_ago=1)
    # defer the 30 oldest (as D5 does), keep the newest due
    session.execute(
        text(
            "UPDATE enrichment_jobs SET not_before = :nb, queued_at = :q WHERE item_id <> :i"
        ),
        {"nb": tomorrow, "q": datetime.now(tz=UTC) - timedelta(days=2), "i": str(due.id)},
    )
    session.commit()
    # the premise: 30 not-yet-due queued jobs ahead of the one due job
    counts = session.execute(
        text(
            "SELECT state, COUNT(*) FROM enrichment_jobs "
            "WHERE not_before IS NOT NULL GROUP BY state"
        )
    ).all()
    assert dict(counts) == {"queued": 30}, counts
    service = EnrichmentService(ItemsRepository(session), session)
    report = service.run_once(datetime.now(tz=UTC), limit=20)
    assert report.processed == 1, report  # the due job ran DESPITE 30 deferred ahead
    state = session.execute(
        text("SELECT state FROM enrichment_jobs WHERE item_id = :i"),
        {"i": str(due.id)},
    ).scalar()
    assert state == "done"
    _wipe(session)
    session.close()


def test_worker_sources_parsing_and_domain_validation():
    """Trimmed, lowercased, empties dropped; every value must be in the
    source CHECK domain (taken from the model, never retyped); an unknown
    value FAILS settings load — it must never silently match nothing."""
    s = _settings(enrichment_worker_sources=" EMAIL , email , ")
    assert s.worker_source_list == ("email",)
    s = _settings(enrichment_worker_sources="calendar,whatsapp")
    assert s.worker_source_list == ("calendar", "whatsapp")
    with pytest.raises(ValueError):
        Settings(enrichment_worker_sources="gmail", _env_file=None)
    with pytest.raises(ValueError):
        Settings(enrichment_worker_sources="emails", _env_file=None)
