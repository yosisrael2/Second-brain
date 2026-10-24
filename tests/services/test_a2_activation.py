"""A2 activation seam: one setting + one factory, stub stays the default.

The invariant that must survive the merge: NOTHING changes on live —
``enrichment_worker_enabled`` stays False, the default extractor at every
production construction site stays :class:`StubExtractor`, and only
``ENRICHMENT_EXTRACTOR=llm`` (validated by Settings) switches the real
LLMExtractor in, built on the SAME shared LLM client ``brain extract-eval``
uses. Every production construction site must route through the ONE factory
``build_extractor`` — no other StubExtractor() construction may remain in
src outside it.
"""

from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from brain.config import Settings
from brain.db.session import SessionLocal, engine
from brain.services.extraction.base import StubExtractor
from brain.services.extraction.service import EnrichmentService
from conftest import assert_test_database

_JLM = ZoneInfo("Asia/Jerusalem")


class FakeLLM:
    """Fake LLM client: records prompts, returns one fixed strict-JSON reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> SimpleNamespace:
        self.prompts.append(prompt)
        return SimpleNamespace(text=self.reply)

    def close(self) -> None:
        pass


_VALID_REPLY = (
    '{"candidates": [{"kind": "task", "title": "Bring a ruler", '
    '"quote": "Bring a ruler tomorrow", "when_raw": "tomorrow"}]}'
)


@pytest.fixture(name="session")
def session_fixture(database: None):
    """Real session on the *_test database, wiped around the test."""
    assert_test_database(str(engine.url))
    session = SessionLocal()
    session.execute(text("DELETE FROM enrichment_jobs"))
    session.execute(text("DELETE FROM item_sources"))
    session.execute(text("DELETE FROM memory_items"))
    session.commit()
    yield session
    session.rollback()
    session.close()
    session.execute(text("DELETE FROM enrichment_jobs"))
    session.execute(text("DELETE FROM item_sources"))
    session.execute(text("DELETE FROM memory_items"))
    session.commit()


class TestFactory:
    def test_default_builds_stub(self):
        """enrichment_extractor defaults to 'stub' -> StubExtractor product."""
        from brain.services.extraction.service import build_extractor

        ext = build_extractor(Settings())
        assert isinstance(ext, StubExtractor)
        assert ext.name == "stub-v0"

    def test_llm_builds_llm_extractor_on_the_shared_deps_client(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """'llm' -> LLMExtractor wired to brain.api.deps.get_llm_client()
        (the exact client `brain extract-eval` uses) — no second wiring,
        no network (fake client)."""
        from brain.services.extraction.llm_extractor import LLMExtractor
        from brain.services.extraction.service import build_extractor

        fake = FakeLLM(_VALID_REPLY)
        monkeypatch.setattr("brain.api.deps.get_llm_client", lambda: fake)
        ext = build_extractor(Settings(enrichment_extractor="llm"))
        assert isinstance(ext, LLMExtractor)

        from datetime import UTC, datetime

        from brain.db.models import MemoryItem

        source = MemoryItem(raw_text="Bring a ruler tomorrow", occurred_at=datetime.now(UTC))
        candidates = ext.extract(source, "memory context")
        assert [c.title for c in candidates] == ["Bring a ruler"]
        assert len(fake.prompts) == 1  # the fake client was the one used

    def test_settings_rejects_invalid_value(self):
        """Anything but 'stub'/'llm' is a settings validation error."""
        with pytest.raises(ValidationError):
            Settings(enrichment_extractor="gpt")


class TestDefaultsPinned:
    """The live-safety invariant: defaults unchanged by the seam."""

    def test_worker_off_and_stub_extractor(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ENRICHMENT_EXTRACTOR", raising=False)
        monkeypatch.delenv("ENRICHMENT_WORKER_ENABLED", raising=False)
        settings = Settings()
        assert settings.enrichment_extractor == "stub"
        assert settings.enrichment_worker_enabled is False


class TestProductionSites:
    """Every production construction site routes through the ONE factory."""

    def test_default_sites_get_stub(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ):
        from brain.repositories.items import ItemsRepository

        repo = ItemsRepository(session)
        # The three direct sites (cli brain enrich / ops route / health check):
        # each constructs EnrichmentService WITHOUT an extractor.
        cli_site = EnrichmentService(repo, session, daily_cap=100)
        ops_site = EnrichmentService(repo, session)
        assert isinstance(cli_site._extractor, StubExtractor)
        assert isinstance(ops_site._extractor, StubExtractor)

        # ...and they get it FROM THE FACTORY, not a hardcoded stub:
        sentinel = SimpleNamespace(name="factory-product")
        monkeypatch.setattr(
            "brain.services.extraction.service.build_extractor",
            lambda settings: sentinel,
        )
        wired = EnrichmentService(repo, session, daily_cap=100)
        assert wired._extractor is sentinel

    def test_eval_site_falls_back_to_the_factory(self, session: Session, tmp_path):
        """run_extraction_eval(extractor=None) must produce EXACTLY the same
        rows as an explicit StubExtractor — i.e. it fell back to the factory
        (deterministic stub) rather than breaking or inventing a fourth path."""
        from brain.services.extraction.base import StubExtractor
        from brain.services.extraction.eval_runner import run_extraction_eval

        body = "הגיעו עם כובע לטיול ביום חמישי 22.10. יש להצטייד בכובע."
        emails = tmp_path / "emails.txt"
        emails.write_text(
            "From: Morah Rinat <rinat@example.org>\n"
            "Date: Mon, 19 Oct 2026 09:00\n"
            f"\n{body}\n",
            encoding="utf-8",
        )
        explicit = run_extraction_eval(
            emails, tmp_path / "a.jsonl", session, StubExtractor(), tz=_JLM
        )
        fallback = run_extraction_eval(
            emails, tmp_path / "b.jsonl", session, None, tz=_JLM
        )
        assert fallback == explicit
        assert fallback, "the eval site must still extract via the fallback"


class TestSurfacing:
    """Health/ops report WHICH extractor is configured."""

    def test_ops_enrichment_route_reports_extractor(self, client: TestClient):
        payload = client.get("/ops/enrichment").json()
        assert payload["extractor"] == "stub"

    def test_health_enrichment_reports_extractor(self, client: TestClient):
        body = client.get("/ops/health").json()
        details = body["components"]["enrichment"]["details"]
        assert details["extractor"] == "stub"
        assert "extractor stub" in body["components"]["enrichment"]["summary"]
