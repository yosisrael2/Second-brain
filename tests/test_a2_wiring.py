"""A2 wiring: the in-bot enrichment worker (default OFF).

When ENRICHMENT_WORKER_ENABLED=true the bot process runs one enrichment pass
per interval as a BACKGROUND task (the poll loop never waits for it) and
delivers proposal cards through the SAME on_children_changed hook the PR C
tests use — translated to cards on the loop thread, never from the worker.
When false (the default) nothing is scheduled and nothing changes.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from brain.config import Settings
from brain.db.models import ItemSource, MemoryItem
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


def _services(enrichment: Any) -> BotServices:
    return BotServices(
        ingest=None, items=None, search=None, ask=None, enrichment=enrichment
    )


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


class _BlockingEnrichment:
    """Fake service: fires the hook, then blocks until released (B1/B2)."""

    def __init__(self, outcomes: list | None = None) -> None:
        self.calls: list[datetime] = []
        self.on_children_changed = None
        self._outcomes = outcomes or []
        self.started = threading.Event()
        self.release = threading.Event()
        self.done = False

    def run_once(self, now, *, limit=20, backfill=False):
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


# --- (a) the default: switch off -> nothing runs ------------------------------


def test_worker_disabled_schedules_nothing(monkeypatch):
    fake = _BlockingEnrichment()
    fake.release.set()

    @contextmanager
    def factory():
        yield _services(fake)

    monkeypatch.setattr("brain.config.get_settings", lambda: _settings())
    core = _bot(factory)
    _fresh_bot_state(core)
    _flush(core, _Tg())
    assert fake.calls == []  # zero passes
    assert core._enrich_task is None  # nothing scheduled
    assert core.drain_outbound() == []  # zero cards


# --- (B1) a running pass never blocks the poll loop ---------------------------


def test_worker_pass_does_not_block_the_flush(monkeypatch):
    fake = _BlockingEnrichment()

    @contextmanager
    def factory():
        yield _services(fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot(factory)
    _fresh_bot_state(core)

    async def go():
        try:
            # the flush must RETURN while the pass is still running
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        except TimeoutError:
            fake.release.set()  # unblock the thread before the loop closes
            raise
        assert fake.started.is_set()  # the pass is running...
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
    fake_sources = []
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
        fake_sources.append(source)
    fake = _BlockingEnrichment(outcomes=_outcomes_for(*fake_sources))

    @contextmanager
    def factory():
        yield _services(fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot(factory)
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
    assert "EnrichmentService" not in src


# --- (c) a raising pass is logged + ops event; the bot keeps running ----------


def test_worker_pass_error_is_recorded_and_bot_survives(monkeypatch):
    events: list[dict] = []

    class _Raising:
        def __init__(self) -> None:
            self.on_children_changed = None
            self.calls = 0

        def run_once(self, now, *, limit=20, backfill=False):
            self.calls += 1
            raise RuntimeError("boom")

    fake = _Raising()

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
    core = _bot(factory)
    _fresh_bot_state(core)

    async def go():
        await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
        await _await_pass_done(core)
        # the harvest turns the task's exception into an ops event
        await _flush_bot_jobs(_Tg(), core)
        assert any(
            e["name"] == "worker_pass_failed" and e["ok"] is False for e in events
        )
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

    @contextmanager
    def factory():
        yield _services(fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot(factory)
    _fresh_bot_state(core)

    async def go():
        try:
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
            assert fake.started.is_set()
            await asyncio.wait_for(_flush_bot_jobs(_Tg(), core), timeout=2.0)
            assert not fake.done  # still the SAME pass in flight
            assert len(fake.calls) == 1  # no second pass started
        finally:
            fake.release.set()
        await _await_pass_done(core)

    asyncio.run(go())


# --- (e) the interval gates consecutive passes --------------------------------


def test_worker_pass_respects_the_interval(monkeypatch):
    fake = _BlockingEnrichment()
    fake.release.set()  # passes finish instantly

    @contextmanager
    def factory():
        yield _services(fake)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot(factory)
    _fresh_bot_state(core)

    async def go():
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

    @contextmanager
    def factory():
        yield _services(service)

    monkeypatch.setattr(
        "brain.config.get_settings", lambda: _settings(enrichment_worker_enabled=True)
    )
    core = _bot(factory)
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
