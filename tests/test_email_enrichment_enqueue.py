"""A2 enablement seam, email path (fix/email-enqueue-enrichment).

The gmail-poll cycle (`cli._gmail_poll_cycle` — the ONE live email ingest
path) must enqueue a durable daily-budget extraction job for every NEWLY
ingested email item, gated on the worker switch + the week-1 source scope.
The 198 already-ingested email items must stay job-less (no backfill by
side effect). Google, the keychain, the LLM and Qdrant are faked; everything
runs against the *_test database.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

import brain.cli as cli
import brain.config as config
from brain.connectors.base import FetchResult
from brain.connectors.testing import FakeConnector, RecordingIndexer, make_record
from brain.db.session import SessionLocal, engine
from conftest import assert_test_database

assert_test_database(str(engine.url))


@pytest.fixture(autouse=True)
def _wipe_email_state(database: None) -> None:
    """Isolation: each test starts from an empty items/ledger/queue (the
    database fixture migrates once per session; rows persist across tests)."""
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM enrichment_jobs"))
        conn.execute(text("DELETE FROM connector_records"))
        conn.execute(text("DELETE FROM connector_state"))
        conn.execute(text("DELETE FROM memory_items"))


EMAIL_BODY = (
    "שלום,\nמחר כינוס הורים באמפי ב-22/10 בשעה 20:00, "
    "וביום חמישי 23/10 חולצה לבנה.\n"
)


class _EmptyClassifier:
    """IngestService classifier seam: the classifier-fallback result (nothing
    to apply — the item keeps its defaults, like an unavailable classifier)."""

    def classify(self, text: str) -> dict[str, Any]:
        return {
            "type": None,
            "project": None,
            "importance": None,
            "actionability": None,
            "confidence": None,
        }


class _NoDrainAttachments:
    """The cycle calls attachments.drain() once per cycle; record nothing."""

    def drain(self) -> int:
        return 0


def _email_settings(**kw: Any) -> config.Settings:
    return config.Settings(
        gmail_min_call_interval_s=0.0,
        gmail_label_id="Label_1",
        enrichment_worker_enabled=kw.pop("worker_enabled", True),
        enrichment_worker_sources=kw.pop("sources", "email"),
        enrichment_extractor=kw.pop("extractor", "stub"),
        enrichment_daily_cap=20,
        _env_file=None,
        **kw,
    )


def _new_email_record(external_id: str = "m1") -> Any:
    return make_record(
        external_id=external_id,
        source="email",
        title="כינוס הורים",
        text=EMAIL_BODY,
    )


def _batch(records: list[Any]) -> FetchResult:
    return FetchResult(records=records, next_cursor=None)


def _run_cycle(monkeypatch, settings: config.Settings, batches: list[Any]) -> Any:
    """Run ONE real _gmail_poll_cycle with faked seams; return (summary, jobs, items)."""
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("brain.api.deps.get_classifier", lambda: _EmptyClassifier())
    indexer = RecordingIndexer()
    monkeypatch.setattr("brain.api.deps.get_item_indexer", lambda: indexer)
    connector = FakeConnector(name="email", batches=list(batches))
    monkeypatch.setattr(cli, "_build_connector", lambda name, session=None, **kw: connector)
    monkeypatch.setattr(cli, "_side_process_indexer", lambda: object())
    monkeypatch.setattr(
        cli, "_cli_attachment_service", lambda settings, indexer: _NoDrainAttachments()
    )

    summary = cli._gmail_poll_cycle(object(), _NoDrainAttachments())

    with SessionLocal() as session:
        jobs = session.execute(
            text("SELECT item_id, state, budget FROM enrichment_jobs ORDER BY id")
        ).fetchall()
        items = session.execute(
            text("SELECT id FROM memory_items WHERE source = 'email'")
        ).fetchall()
    return summary, [tuple(j) for j in jobs], [tuple(i) for i in items]


class TestGmailPollEnqueue:
    def test_new_email_ingest_enqueues_one_daily_job(
        self, monkeypatch, database
    ):
        """Rule 1 (RED on main): a cycle that ingests a NEW email item must
        create exactly one enrichment job for it — state=queued (the only
        state the worker drains), budget=daily (the live cap budget)."""
        summary, jobs, items = _run_cycle(
            monkeypatch,
            _email_settings(worker_enabled=True),
            [_batch([_new_email_record("m1")])],
        )
        assert summary.state == "ok"
        assert len(items) == 1, "the cycle must ingest the email record"
        assert len(jobs) == 1, f"expected 1 queued job for the new email, got {jobs}"
        job = jobs[0]
        assert job[1] == "queued", f"the job must be queued, got {job}"
        assert job[2] == "daily", f"the job must carry the daily budget, got {job}"

    def test_repoll_of_the_same_message_enqueues_no_duplicate(
        self, monkeypatch, database
    ):
        """Rule 1: re-polling an already-ingested message (same version — the
        ledger dedupes it) must NOT create a second job. Two cycles, one
        record, one job."""
        record = _new_email_record("m1")
        _run_cycle(monkeypatch, _email_settings(worker_enabled=True), [_batch([record])])
        # second poll: the same record re-listed, unchanged version -> deduped
        _, jobs, _items = _run_cycle(
            monkeypatch, _email_settings(worker_enabled=True), [_batch([])]
        )
        assert len(jobs) == 1, f"a re-poll must not enqueue again, got {jobs}"

    def test_worker_off_enqueues_nothing(self, monkeypatch, database):
        """Rule 4: stub extractor + worker OFF (today's live config) must
        behave exactly as before the wiring — zero jobs, ever."""
        _, jobs, items = _run_cycle(
            monkeypatch,
            _email_settings(worker_enabled=False),
            [_batch([_new_email_record("m1")])],
        )
        assert len(items) == 1, "the item is still ingested normally"
        assert len(jobs) == 0, f"worker off must enqueue nothing, got {jobs}"

    def test_sources_without_email_enqueue_nothing(
        self, monkeypatch, database
    ):
        """Rule 2: worker ON but scoped away from email (sources='whatsapp')
        must enqueue NOTHING for email items — otherwise the moment the scope
        widens, the accumulated queue IS a backfill."""
        _, jobs, items = _run_cycle(
            monkeypatch,
            _email_settings(worker_enabled=True, sources="whatsapp"),
            [_batch([_new_email_record("m1")])],
        )
        assert len(items) == 1, "the item is still ingested"
        assert len(jobs) == 0, (
            f"email out of the worker scope must enqueue nothing, got {jobs}"
        )

    def test_existing_items_are_never_enqueued(self, monkeypatch, database):
        """Rule 3: already-ingested email items (the live 198) never gain a
        job — not on turn-on, not after a restart. A poll cycle that ingests
        nothing new must leave the queue empty (the ONLY enqueue site is the
        fresh-ingest path; nothing scans memory_items)."""
        from brain.db.models import MemoryItem

        with SessionLocal() as session:
            for _ in range(2):
                session.add(MemoryItem(raw_text=EMAIL_BODY, source="email"))
            session.commit()

        _, jobs, items = _run_cycle(
            monkeypatch, _email_settings(worker_enabled=True), [_batch([])]
        )
        assert len(items) == 2, "the seed items are untouched"
        assert len(jobs) == 0, f"no enqueue without a NEW ingest, got {jobs}"

    def test_cycle_enrichment_service_uses_the_worker_seams(
        self, monkeypatch, database
    ):
        """Rule 5: the poll cycle builds its EnrichmentService the same way
        the worker builds the pass service — the DEFAULT extractor (the A2
        settings seam; never a second LLM wiring) — for both seam values, on
        the shared indexer."""
        from brain.api import deps as deps_mod
        from brain.repositories.items import ItemsRepository
        from brain.services.extraction.base import StubExtractor
        from brain.services.extraction.llm_extractor import LLMExtractor
        from brain.services.memory import MemoryService

        sentinel = object()
        monkeypatch.setattr(deps_mod, "get_llm_client", lambda: sentinel)
        shared_indexer = RecordingIndexer()
        monkeypatch.setattr("brain.api.deps.get_item_indexer", lambda: shared_indexer)

        with SessionLocal() as session:
            repo = ItemsRepository(session)
            settings_stub = _email_settings(worker_enabled=True, extractor="stub")
            monkeypatch.setattr("brain.config.get_settings", lambda: settings_stub)
            service = cli._build_poll_enrichment(repo, session, settings_stub)
            assert isinstance(service._extractor, StubExtractor), (
                "the poller's service must use the DEFAULT extractor seam"
            )
            assert isinstance(service._memory, MemoryService), (
                "the worker passes memory compaction; the poller must too"
            )
            assert service._indexer is shared_indexer, (
                "the worker passes the shared indexer; the poller must too"
            )

            settings_llm = _email_settings(worker_enabled=True, extractor="llm")
            monkeypatch.setattr("brain.config.get_settings", lambda: settings_llm)
            service_llm = cli._build_poll_enrichment(
                repo, session, settings_llm
            )
            assert isinstance(service_llm._extractor, LLMExtractor)
            assert service_llm._extractor._llm is sentinel, (
                "the poller must go through the ONE shared LLM client"
            )


class TestWorkerPassBuilds:
    def test_build_pass_service_imports_memory_service_correctly(
        self, monkeypatch, database
    ):
        """BLOCKER for the A2 GOAL (RED on main): telegram_bot._build_pass_service
        imports MemoryService from brain.repositories.memory — the class lives
        in brain.services.memory. The suite never noticed because every worker
        test monkeypatches this seam. The first REAL worker pass (ENRICHMENT_
        WORKER_ENABLED=true) would crash with ImportError before draining
        anything. The fix is the import path only."""

        monkeypatch.setattr(
            "brain.api.deps.get_item_indexer", lambda: RecordingIndexer()
        )
        from brain.repositories.items import ItemsRepository
        from brain.services.extraction.service import EnrichmentService
        from brain.services.memory import MemoryService
        from brain.telegram_bot import _build_pass_service

        with SessionLocal() as session:
            service = _build_pass_service(_email_settings(), session)
        assert isinstance(service, EnrichmentService)
        assert isinstance(service._memory, MemoryService), (
            "the pass service must carry memory compaction"
        )
        assert isinstance(service._repo, ItemsRepository)
