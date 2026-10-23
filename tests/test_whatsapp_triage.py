"""PR D2a tests: WhatsApp relevance triage (design delta item 36).

Each conversation (source='whatsapp') is labelled relevant | noise — Jev
first (one typed choice call), the LLM only when Jev's confidence is low.
Noise stays stored and searchable but is hidden from the default listings
and never enqueued for enrichment. Offline: fakes for Jev/LLM; real
MemoryService + ItemsRepository over brain_w5_test.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text as sql_text
from tests.conftest import assert_test_database

from brain.ai.jev import JevChoice, JevError
from brain.config import get_settings
from brain.db.models import MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.services.triage import (
    JEV_CONFIDENT_BAND,
    TRIAGE_VERSION,
    TriageResult,
    triage_conversation,
)


def _wipe_items() -> None:
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM memory_items"))


@pytest.fixture(autouse=True)
def _clean_items(database: None) -> Iterator[None]:
    """Wipe items around each test (after the schema-creating `database`)."""
    _wipe_items()
    yield
    _wipe_items()


class FakeJev:
    """Jev stand-in: a scripted choice or a raised JevError."""

    def __init__(self, choice: JevChoice | Exception) -> None:
        self._choice = choice
        self.calls = 0

    def choice(self, question: str, options, state) -> JevChoice:
        self.calls += 1
        if isinstance(self._choice, Exception):
            raise self._choice
        return self._choice


class FakeLLM:
    """LLM stand-in: a scripted completion or a raised error."""

    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str):
        self.prompts.append(prompt)
        if isinstance(self._reply, Exception):
            raise self._reply

        class _Completion:
            text = self._reply
            model = "fake-llm"

        return _Completion()


def _conversation(
    text: str = "[WhatsApp · ב' ברקת - הורים]\n[21/10/2026, 18:23] מעין: מחר חולצה לבנה",
) -> MemoryItem:
    return MemoryItem(
        raw_text=text,
        source="whatsapp",
        occurred_at=datetime(2026, 10, 21, 15, 23, tzinfo=UTC),
        meta={"kind": "whatsapp_conversation", "channel": "ב' ברקת - הורים"},
    )


# --- the triage decision itself -------------------------------------------------


def test_jev_confident_path_never_calls_the_llm() -> None:
    jev = FakeJev(JevChoice(option="noise", confidence=0.97))
    llm = FakeLLM('{"label": "relevant", "confidence": 0.9}')

    result = triage_conversation("Thanks! 🙏😊", jev=jev, llm=llm)

    assert result == TriageResult(
        label="noise", confidence=0.97, method="jev", version=TRIAGE_VERSION
    )
    assert jev.calls == 1 and llm.prompts == [], "a confident Jev must not cost an LLM call"


# --- the deterministic kid override (v3) ---------------------------------------


def test_kid_named_override_is_relevant_without_any_model_call() -> None:
    jev = FakeJev(JevError("jev down"))  # must never be reached
    llm = FakeLLM(RuntimeError("llm down"))  # must never be reached

    result = triage_conversation(
        "[WhatsApp · ב' ברקת - הורים]\n[21/10/2026, 18:23] אמא של יואב: מי לוקח את איתמר היום?",
        jev=jev,
        llm=llm,
        kid_names=["Itamar", "איתמר"],
    )

    assert result == TriageResult(
        label="relevant", confidence=1.0, method="kid_named", version=TRIAGE_VERSION
    )
    assert jev.calls == 0 and llm.prompts == [], "the override never asks a model"


def test_kid_override_uses_the_shared_matcher_rules() -> None:
    jev = FakeJev(JevChoice(option="noise", confidence=0.99))

    # 'לאיתמר' (glue prefix) fires the override...
    fired = triage_conversation(
        "מה צריך לאיתמר מחר?", jev=jev, llm=FakeLLM("unused"), kid_names=["איתמר"]
    )
    assert fired is not None and fired.method == "kid_named"

    # ...but a sub-word never does ('שחר' inside 'שחרור')
    jev2 = FakeJev(JevChoice(option="noise", confidence=0.99))
    not_fired = triage_conversation(
        "השחרור של הסרט החדש", jev=jev2, llm=FakeLLM("unused"), kid_names=["שחר"]
    )
    assert not_fired is not None and not_fired.method != "kid_named"


def test_confirmed_kid_names_uses_the_ask_kid_definition() -> None:
    from brain.services.triage import confirmed_kid_names

    entities = [
        {"kind": "person", "name": "Itamar", "aliases": ["איתמר"], "facts": [
            {"predicate": "grade", "value": "2"},
        ]},
        {"kind": "person", "name": "NoGrade", "aliases": ["x"], "facts": []},
        {"kind": "channel", "name": "ב' ברקת - הורים", "facts": []},
    ]
    assert confirmed_kid_names(entities) == ["Itamar", "איתמר"]


def test_v3_prompt_carries_the_new_relevance_rules() -> None:
    """The v3 additions (staff/health, new groups) are pinned in the prompt."""
    from brain.services import triage

    prompt = triage._PROMPT_TEMPLATE.format(text="x", version=triage.TRIAGE_VERSION)
    assert "staff changes / absences / illness" in prompt
    assert "new parents' groups, registrations or sign-ups" in prompt
    assert triage.TRIAGE_VERSION == "triage-v3"
    # v2's noise categories stay
    assert "lost & found" in prompt and "media-only" in prompt
    assert "other families" in prompt


def test_llm_fallback_when_jev_confidence_is_low() -> None:
    jev = FakeJev(JevChoice(option="relevant", confidence=JEV_CONFIDENT_BAND - 0.01))
    llm = FakeLLM('{"label": "noise", "confidence": 0.91}')

    result = triage_conversation("[WhatsApp · chat] messages...", jev=jev, llm=llm)

    assert result.label == "noise"
    assert result.method == "llm"
    assert result.confidence == pytest.approx(0.91)
    assert len(llm.prompts) == 1, "exactly ONE LLM call per conversation"


def test_llm_fallback_below_the_band_only() -> None:
    jev = FakeJev(JevChoice(option="relevant", confidence=JEV_CONFIDENT_BAND))

    result = triage_conversation("text", jev=jev, llm=FakeLLM("unused"))

    assert result.method == "jev", "confidence exactly at the band is confident enough"


def test_both_fail_leaves_the_item_untriaged() -> None:
    jev = FakeJev(JevError("jev down"))
    llm = FakeLLM(RuntimeError("llm down"))

    result = triage_conversation("text", jev=jev, llm=llm)

    assert result is None, "a total failure must leave the item untriaged, never guessed"


def test_llm_malformed_answer_leaves_untriaged() -> None:
    jev = FakeJev(JevError("jev down"))
    llm = FakeLLM("I think this is relevant!!!")  # not the strict JSON contract

    result = triage_conversation("text", jev=jev, llm=llm)

    assert result is None, "a malformed LLM answer is a failure, never a guess"


def test_llm_rejects_unknown_labels() -> None:
    jev = FakeJev(JevError("jev down"))
    llm = FakeLLM('{"label": "spam", "confidence": 0.99}')

    assert triage_conversation("text", jev=jev, llm=llm) is None


# --- storage on the item ----------------------------------------------------------


def test_apply_writes_label_confidence_method_version() -> None:
    _wipe_items()
    session = SessionLocal()
    repo = ItemsRepository(session)
    item = repo.add(_conversation())
    result = TriageResult(label="noise", confidence=0.97, method="jev", version=TRIAGE_VERSION)

    repo.apply_triage(item, result)
    session.commit()

    stored = repo.get(item.id)
    assert stored is not None
    assert stored.triage_label == "noise"
    assert stored.triage_confidence == pytest.approx(0.97)
    assert stored.triage_method == "jev"
    assert stored.triage_version == TRIAGE_VERSION
    session.close()


def test_retriage_overwrites_the_previous_label() -> None:
    _wipe_items()
    session = SessionLocal()
    repo = ItemsRepository(session)
    item = repo.add(_conversation())
    repo.apply_triage(item, TriageResult("noise", 0.9, "jev", TRIAGE_VERSION))
    repo.apply_triage(item, TriageResult("relevant", 0.95, "llm", TRIAGE_VERSION))
    session.commit()

    stored = repo.get(item.id)
    assert stored is not None
    assert stored.triage_label == "relevant" and stored.triage_method == "llm"
    session.close()


# --- listing vs search ------------------------------------------------------------


def test_noise_hidden_from_listings_but_found_by_search() -> None:
    _wipe_items()
    session = SessionLocal()
    repo = ItemsRepository(session)
    noise = repo.add(_conversation("[WhatsApp · ח] מישהו: תודה רבה!! 🙏"))
    relevant = repo.add(_conversation("[WhatsApp · ב] מעין: מחר חולצה לבנה וקלמר"))
    repo.apply_triage(noise, TriageResult("noise", 0.97, "jev", TRIAGE_VERSION))
    repo.apply_triage(relevant, TriageResult("relevant", 0.95, "jev", TRIAGE_VERSION))
    session.commit()

    listed = {item.id for item in repo.list(
        item_type=None, project=None, status=None, limit=50, offset=0
    )}
    assert noise.id not in listed, "noise must be hidden from the default listing"
    assert relevant.id in listed

    searched = {item.id for item in repo.search(query="תודה", limit=50, offset=0)}
    assert noise.id in searched, "noise stays stored and SEARCHABLE"

    with_noise = {item.id for item in repo.list(
        item_type=None, project=None, status=None, limit=50, offset=0, include_noise=True
    )}
    assert noise.id in with_noise, "an explicit opt-in lists noise again"
    session.close()


def test_untriaged_items_are_never_hidden() -> None:
    _wipe_items()
    session = SessionLocal()
    repo = ItemsRepository(session)
    item = repo.add(_conversation())
    session.commit()

    listed = {
        i.id for i in repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
    }
    assert item.id in listed, "untriaged (NULL) items stay visible"
    session.close()


def test_non_whatsapp_noise_label_impossible_to_leak_into_listing_filter() -> None:
    """A non-WhatsApp item with a triage label (defensive) is never hidden."""
    _wipe_items()
    session = SessionLocal()
    repo = ItemsRepository(session)
    email_item = repo.add(MemoryItem(
        raw_text="email body", source="email",
        occurred_at=datetime(2026, 10, 21, tzinfo=UTC),
    ))
    repo.apply_triage(email_item, TriageResult("noise", 0.9, "jev", TRIAGE_VERSION))
    session.commit()

    listed = {
        i.id for i in repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
    }
    assert email_item.id in listed, "only WHATSAPP noise is hidden"
    session.close()


# --- enrichment eligibility ---------------------------------------------------------


def test_enrichment_never_enqueues_noise() -> None:
    from brain.services.extraction.service import qualifies

    # the text carries two date expressions, so it WOULD qualify on its own
    def qualifying_conversation() -> MemoryItem:
        return _conversation(
            "[WhatsApp · ב' ברקת - הורים]\n[21/10/2026, 18:23] מעין: מחר חולצה לבנה "
            "וב-25/10/2026 טיול"
        )

    noise = qualifying_conversation()
    noise.triage_label = "noise"
    relevant = qualifying_conversation()
    relevant.triage_label = "relevant"
    untriaged = qualifying_conversation()

    assert qualifies(noise) is False, "noise is never enqueued for extraction"
    assert qualifies(relevant) is True
    assert qualifies(untriaged) is True, "untriaged behaves exactly as before"


# --- the poller hook -----------------------------------------------------------------


def test_conversation_create_and_update_are_triaged() -> None:
    """The hook triages on create AND re-triages when a conversation grows."""
    from brain.services.whatsapp import WhatsAppPoller  # noqa: PLC0415

    triaged: list[str] = []

    def fake_trier(item: MemoryItem) -> TriageResult | None:
        triaged.append(item.raw_text[:20])
        item.triage_label = "relevant" if "טיול" in item.raw_text else "noise"
        item.triage_method = "jev"
        item.triage_version = TRIAGE_VERSION
        return TriageResult(item.triage_label, 0.95, "jev", TRIAGE_VERSION)

    # the poller needs the bridge client only for HTTP paths — the triage
    # hook is exercised through the persistence internals here
    service = WhatsAppPoller.__new__(WhatsAppPoller)
    service._trier = fake_trier  # type: ignore[attr-defined]
    service._items = None  # type: ignore[attr-defined]  # persistence covered elsewhere

    item = _conversation()
    service._triage_item(item)  # type: ignore[attr-defined]
    assert triaged and item.triage_label == "noise"

    item.raw_text += "\n[21/10/2026, 19:00] מעין: הטיול השני מתבטל"
    service._triage_item(item)  # type: ignore[attr-defined]
    assert len(triaged) == 2, "an updated conversation is re-triaged"
    assert item.triage_label == "relevant", "a conversation can turn relevant"


def test_triage_failure_is_recorded_never_silent(monkeypatch) -> None:
    from brain.services import whatsapp as wa  # noqa: PLC0415
    from brain.services.whatsapp import WhatsAppPoller  # noqa: PLC0415

    events: list[dict] = []
    monkeypatch.setattr(
        wa.ops, "record_event",
        lambda component, kind, **kw: events.append({"component": component, "kind": kind, **kw}),
    )

    def failing_trier(item: MemoryItem) -> TriageResult | None:
        return None

    service = WhatsAppPoller.__new__(WhatsAppPoller)
    service._trier = failing_trier  # type: ignore[attr-defined]
    service._items = None  # type: ignore[attr-defined]
    item = _conversation()

    service._triage_item(item)  # type: ignore[attr-defined]  # must not raise

    assert events and events[0]["component"] == "triage" and events[0]["ok"] is False
    assert item.triage_label is None, "a failed triage leaves the item untriaged"


# --- the backfill command -------------------------------------------------------------


def test_backfill_command_refuses_the_live_store_without_the_flag(monkeypatch) -> None:
    from brain import cli

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://brain:brain@localhost:27432/brain")
    monkeypatch.delenv("BRAIN_ALLOW_LIVE", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="LIVE"):
            cli.main(["whatsapp-triage", "--limit", "10"])
    finally:
        get_settings.cache_clear()


def test_backfill_triages_untriaged_whatsapp_items_only(monkeypatch) -> None:
    from brain import cli

    _wipe_items()
    session = SessionLocal()
    repo = ItemsRepository(session)
    untriaged = repo.add(_conversation("[WhatsApp · ב] מעין: מחר חולצה"))
    already = repo.add(_conversation("[WhatsApp · ג] ח: תודה 🙏"))
    repo.apply_triage(already, TriageResult("relevant", 0.9, "jev", "triage-v0"))
    email_item = repo.add(MemoryItem(
        raw_text="email", source="email", occurred_at=datetime(2026, 10, 21, tzinfo=UTC)
    ))
    session.commit()

    def fake_trier(item: MemoryItem) -> TriageResult | None:
        label = "noise" if "תודה" in item.raw_text else "relevant"
        return TriageResult(label, 0.95, "jev", TRIAGE_VERSION)

    monkeypatch.setattr("brain.cli._build_trier", lambda session: fake_trier)
    rc = cli.main(["whatsapp-triage", "--limit", "100"])
    assert rc == 0

    session.expire_all()
    assert repo.get(untriaged.id).triage_label == "relevant"
    assert repo.get(already.id).triage_version == "triage-v0", "already-triaged untouched"
    assert repo.get(email_item.id).triage_label is None, "non-WhatsApp never triaged"
    session.close()


# --- ops visibility ---------------------------------------------------------------------


def test_triage_records_one_ops_event_per_decision(monkeypatch) -> None:
    from brain.services import triage as triage_module

    events: list[dict] = []
    monkeypatch.setattr(
        triage_module.ops, "record_event",
        lambda component, kind, **kw: events.append({"component": component, "kind": kind, **kw}),
    )
    jev = FakeJev(JevChoice(option="noise", confidence=0.97))

    triage_conversation("thanks 🙏", jev=jev, llm=FakeLLM("unused"))

    assert events and events[0]["component"] == "triage"
    assert events[0]["ok"] is True
    detail = events[0]["detail"]
    assert detail["label"] == "noise" and detail["method"] == "jev"
    assert isinstance(detail["confidence"], float)


def test_health_reports_today_triage_counts(monkeypatch) -> None:
    from datetime import datetime as dt

    from brain import ops
    from brain.ops.health import run_health_checks

    today = dt.now(UTC).isoformat()
    monkeypatch.setattr(
        ops, "record_event",
        lambda *a, **kw: None,
    )

    def _counts() -> tuple[int, int]:
        summary = run_health_checks(use_cache=False)["components"]["triage"]["summary"]
        # summary shape: 'relevant N / noise M today'
        relevant, noise = summary.split(" / ")
        return int(relevant.split()[1]), int(noise.split()[1])

    relevant_before, noise_before = _counts()
    # two decisions today: 1 relevant + 1 noise
    with engine.begin() as conn:
        conn.execute(sql_text(
            "INSERT INTO ops_events (component, kind, ok, ts, detail) VALUES "
            "('triage', 'decided', true, :ts, '{\"label\": \"relevant\"}'), "
            "('triage', 'decided', true, :ts, '{\"label\": \"noise\"}')"
        ), {"ts": today})

    relevant_after, noise_after = _counts()
    assert relevant_after == relevant_before + 1
    assert noise_after == noise_before + 1
