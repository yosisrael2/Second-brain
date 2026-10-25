"""PR D2a tests: WhatsApp relevance triage (design delta item 36).

Each conversation (source='whatsapp') is labelled relevant | noise — Jev
first (one typed choice call), the LLM only when Jev's confidence is low.
Noise stays stored and searchable but is hidden from the default listings
and never enqueued for enrichment. Offline: fakes for Jev/LLM; real
MemoryService + ItemsRepository over brain_w5_test.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

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


def test_kid_named_method_fits_the_column_and_commits() -> None:
    """Red-first (review round 2, bug 1): 'kid_named' is 9 chars but the
    triage_method column was String(8) — the commit raised 'value too
    long', which in the poller fails the whole channel poll and in the
    backfill CLI aborts the run."""
    _wipe_items()
    session = SessionLocal()
    repo = ItemsRepository(session)
    item = repo.add(_conversation())
    repo.apply_triage(
        item, TriageResult("relevant", 1.0, "kid_named", TRIAGE_VERSION)
    )
    session.commit()  # must not raise StringDataRightTruncation

    stored = repo.get(item.id)
    assert stored is not None
    assert stored.triage_label == "relevant"
    assert stored.triage_method == "kid_named"
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


def test_update_never_downgrades_relevant_to_noise(monkeypatch) -> None:
    """Red-first (review round 2, bug 3): re-triage on UPDATE must never hide
    something real — appending chatter ('תודה 🙏') to a RELEVANT conversation
    keeps it relevant. noise->relevant stays allowed (the other test); the
    blocked downgrade is still recorded as an ops event, never silent."""
    from brain.services import whatsapp as wa  # noqa: PLC0415
    from brain.services.whatsapp import WhatsAppPoller  # noqa: PLC0415

    events: list[dict] = []
    monkeypatch.setattr(
        wa.ops, "record_event",
        lambda component, kind, **kw: events.append(
            {"component": component, "kind": kind, **kw}
        ),
    )

    def trier(item: MemoryItem) -> TriageResult | None:
        # first decision: relevant (a real school notice), then the thread
        # degrades to chatter and the trier says noise on the update
        return TriageResult(
            "noise" if "תודה" in item.raw_text else "relevant", 0.97, "jev", TRIAGE_VERSION
        )

    service = WhatsAppPoller.__new__(WhatsAppPoller)
    service._trier = trier  # type: ignore[attr-defined]
    service._items = None  # type: ignore[attr-defined]

    item = _conversation("[WhatsApp · ב] מעין: מחר חולצה לבנה וקלמר")
    service._triage_item(item)  # type: ignore[attr-defined]
    assert item.triage_label == "relevant"

    item.raw_text += "\n[21/10/2026, 19:00] מעין: תודה 🙏"
    service._triage_item(item)  # type: ignore[attr-defined]  # trier now says noise

    assert item.triage_label == "relevant", "relevant may never become noise on update"
    assert item.triage_method == "jev", "the first (relevant) decision stands"
    blocked = [e for e in events if e["kind"] == "downgrade_blocked"]
    assert blocked, "the blocked downgrade is recorded, never silent"


def test_poll_failure_after_triage_rolls_back_the_triaged_conversation(tmp_path) -> None:
    """Red-first (review round 2, bug 2): the triage hook must not commit
    mid-poll. A failure AFTER triage in the same poll (here: the poller's
    state/cursor bookkeeping) must roll the conversation back with the rest
    of the channel's writes — the poll's single commit point owns the triage
    columns too."""
    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        CHAT_A_JID,
        ZONE,
        FakeIndexer,
        _add_row,
        _bridge_row,
        _write_bridge,
    )
    from tests.services.test_whatsapp_ingest import (
        DEFAULT_GAP_MINUTES as INGEST_GAP,
    )

    from brain.repositories.memory import MemoryRepository  # noqa: PLC0415
    from brain.repositories.whatsapp import WhatsAppStateRepository  # noqa: PLC0415
    from brain.services.memory import MemoryService  # noqa: PLC0415
    from brain.services.whatsapp import BridgeStore, WhatsAppPoller  # noqa: PLC0415

    _wipe_items()
    session = SessionLocal()
    try:
        session.execute(sql_text("DELETE FROM whatsapp_poll_state"))
        session.commit()
        memory_repo = MemoryRepository(session)
        MemoryService(memory_repo).set_channel("Chat A", kind="teacher", applies_to="me")

        path = tmp_path / "messages.db"
        _write_bridge(
            path,
            [(CHAT_A_JID, "Chat A")],
            [_bridge_row("m1", datetime(2026, 10, 22, 12, 0, tzinfo=UTC), content="hello")],
        )

        def trier(item: MemoryItem) -> TriageResult | None:
            return TriageResult("relevant", 0.95, "jev", TRIAGE_VERSION)

        class _BoomState:
            """Delegates to the real state repo; record_poll succeeds for the
            init poll, then always raises — a failure strictly AFTER the
            conversation was triaged in a later poll."""

            def __init__(self, inner: WhatsAppStateRepository) -> None:
                self._inner = inner
                self._calls = 0

            def __getattr__(self, name: str):
                return getattr(self._inner, name)

            def record_poll(self, *args, **kwargs):
                self._calls += 1
                if self._calls >= 2:
                    raise RuntimeError("state write down")
                return self._inner.record_poll(*args, **kwargs)

        poller = WhatsAppPoller(
            bridge=BridgeStore(path),
            state=_BoomState(WhatsAppStateRepository(session)),
            items=ItemsRepository(session),
            memory=MemoryRepository(session),
            indexer=FakeIndexer(),
            trier=trier,
            zone_name=ZONE,
            gap_minutes=INGEST_GAP,
            commit=True,
        )
        poller.poll(now=datetime(2026, 10, 22, 12, 1, tzinfo=UTC))  # init at the live edge
        _add_row(
            path,
            _bridge_row("m2", datetime(2026, 10, 22, 13, 0, tzinfo=UTC), content="fresh"),
        )
        results = poller.poll(now=datetime(2026, 10, 22, 13, 5, tzinfo=UTC))
        assert results == [], "the failed channel contributes no result"

        session.expire_all()
        count = session.execute(sql_text("SELECT count(*) FROM memory_items")).scalar_one()
        assert count == 0, (
            "the triaged conversation must roll back with the failed poll, "
            "not stay committed ahead of the cursor"
        )
    finally:
        session.rollback()
        session.close()


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


def test_health_noise_today_excludes_blocked_downgrades(monkeypatch) -> None:
    """#49 follow-up: a blocked downgrade must not count as noise.

    The trier records 'decided' (label noise) BEFORE the poller's
    keep-relevant guard rejects the apply and records 'downgrade_blocked' —
    one blocked pass = exactly one decided(noise) + one downgrade_blocked.
    Health's 'noise today' must count only APPLIED decisions, i.e. subtract
    the blocked ones."""
    from brain import ops  # noqa: PLC0415
    from brain.ops.health import run_health_checks
    from brain.services.whatsapp import WhatsAppPoller  # noqa: PLC0415

    def _noise_today() -> int:
        summary = run_health_checks(use_cache=False)["components"]["triage"]["summary"]
        return int(summary.split(" / ")[1].split()[1])  # 'relevant N / noise M today'

    noise_before = _noise_today()

    # the REAL event flow: triage_conversation records decided(noise), the
    # poller hook then keeps the item relevant and records downgrade_blocked
    jev = FakeJev(JevChoice(option="noise", confidence=0.97))

    def trier(item: MemoryItem) -> TriageResult | None:
        return triage_conversation(item.raw_text, jev=jev, llm=FakeLLM("unused"))

    service = WhatsAppPoller.__new__(WhatsAppPoller)
    service._trier = trier  # type: ignore[attr-defined]
    service._items = None  # type: ignore[attr-defined]
    item = _conversation("[WhatsApp · ב] מעין: מחר חולצה לבנה וקלמר")
    item.triage_label = "relevant"  # a previous decision the update must not undo
    service._triage_item(item)  # type: ignore[attr-defined]
    assert item.triage_label == "relevant", "the downgrade is blocked"

    monkeypatch.setattr(ops, "record_event", lambda *a, **kw: None)  # no further writes
    assert _noise_today() == noise_before, (
        "the blocked downgrade must not count in health's 'noise today'"
    )


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


# --- fresh kid names per poll pass (step C) -------------------------------------
# The poller holds ONE trier for the process lifetime; confirmed kid names and
# aliases must be re-read once per POLL PASS (never per conversation — no extra
# query in the hot loop). The refresh query is savepoint-scoped so a failure
# cannot poison the pass transaction (pending captures survive), and a failed
# refresh leaves the last-known names in place (never blank, never fatal).


@pytest.fixture(name="triage_world")
def triage_world_fixture(database: None) -> Iterator[Any]:
    """One shared session with clean memory + whatsapp state per test."""
    from brain.repositories.memory import MemoryRepository  # noqa: PLC0415
    from brain.repositories.whatsapp import WhatsAppStateRepository  # noqa: PLC0415
    from brain.services.memory import MemoryService  # noqa: PLC0415

    assert_test_database(str(engine.url))
    session = SessionLocal()
    try:
        for table in (
            "whatsapp_poll_state",
            "memory_items",
            "memory_facts",
            "entity_aliases",
            "entities",
        ):
            session.execute(sql_text(f"DELETE FROM {table}"))
        session.commit()
        memory = MemoryService(MemoryRepository(session))
        # The allowlist is the channel entities: 'Chat A' must be pollable.
        memory.set_channel("Chat A", kind="teacher", applies_to="me")
        yield SimpleNamespace(
            session=session,
            memory=MemoryService(MemoryRepository(session)),
            memory_repo=MemoryRepository(session),
            items=ItemsRepository(session),
            state=WhatsAppStateRepository(session),
        )
    finally:
        session.rollback()
        session.close()


def _confirmed_kid(memory: Any, name: str) -> Any:
    """A confirmed kid (kind person + grade fact) — the ask/triage definition."""
    memory.set_kid(name, grade="ג")
    entity = memory._repo.find_entity(kind="person", name=name)
    assert entity is not None
    return entity


def _wired_trier(world: Any, monkeypatch: Any, jev: Any, llm: Any) -> Any:
    """cli._build_trier over the world's session with fake clients."""
    from brain.cli import _build_trier  # noqa: PLC0415

    monkeypatch.setattr("brain.api.deps.get_jev_client", lambda: jev)
    monkeypatch.setattr("brain.api.deps.get_llm_client", lambda: llm)
    return _build_trier(world.session)


def _trier_poller(world: Any, bridge: Any, trier: Any) -> Any:
    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        DEFAULT_GAP_MINUTES as INGEST_GAP,
    )
    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        ZONE,
        FakeIndexer,
    )

    from brain.services.whatsapp import WhatsAppPoller  # noqa: PLC0415

    return WhatsAppPoller(
        bridge=bridge,
        state=world.state,
        items=world.items,
        memory=world.memory_repo,
        indexer=FakeIndexer(),
        trier=trier,
        zone_name=ZONE,
        gap_minutes=INGEST_GAP,
        commit=False,  # pending writes stay uncommitted: the savepoint test needs them
    )


def test_poller_refresh_honours_alias_added_after_trier_built(
    triage_world: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """RED-first (step C): an alias added after the trier was built must be
    honoured on the NEXT pass's conversations (kid_named), with no restart."""
    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        CHAT_A_JID,
        _add_row,
        _bridge_row,
        _write_bridge,
    )

    from brain.db.models import EntityAlias  # noqa: PLC0415
    from brain.services.whatsapp import BridgeStore  # noqa: PLC0415

    world = triage_world
    rona = _confirmed_kid(world.memory, "Rona")
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("m1", datetime(2026, 10, 22, 12, 0, tzinfo=UTC), content="שלום")],
    )
    trier = _wired_trier(world, monkeypatch, FakeJev(JevError("skip")), FakeLLM("garbage"))
    poller = _trier_poller(world, BridgeStore(path), trier)

    poller.poll(now=datetime(2026, 10, 22, 12, 1, tzinfo=UTC))  # init at the live edge
    _add_row(
        path,
        _bridge_row(
            "m2", datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
            content="מה צריך לארוז לרונה ליום ראשון?",
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 13, 5, tzinfo=UTC))  # creates conversation 1

    world.memory_repo.add_alias(
        EntityAlias(entity_id=rona.id, alias="רונה", lang="he")
    )
    _add_row(
        path,
        _bridge_row(
            "m3", datetime(2026, 10, 22, 16, 0, tzinfo=UTC),
            content="רונה צריכה להביא כובע מחר",
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 16, 5, tzinfo=UTC))  # creates conversation 2

    world.session.expire_all()
    items = (
        world.session.query(MemoryItem)
        .order_by(MemoryItem.occurred_at.asc())
        .all()
    )
    assert [i.triage_method for i in items] == [None, "kid_named"], (
        "conversation 2 must be kid_named via the refreshed kid names "
        "(the alias was added after the trier was built)"
    )


def test_alias_removed_between_passes_stops_matching(
    triage_world: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """RED-first (step C): refresh REPLACES the kid-name list — an alias
    deleted between passes stops matching on the next pass (no append-only
    staleness in the other direction)."""
    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        CHAT_A_JID,
        _add_row,
        _bridge_row,
        _write_bridge,
    )

    from brain.db.models import EntityAlias  # noqa: PLC0415
    from brain.services.whatsapp import BridgeStore  # noqa: PLC0415

    world = triage_world
    rona = _confirmed_kid(world.memory, "Rona")
    world.memory_repo.add_alias(EntityAlias(entity_id=rona.id, alias="רונה", lang="he"))
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("m1", datetime(2026, 10, 22, 12, 0, tzinfo=UTC), content="שלום")],
    )
    trier = _wired_trier(world, monkeypatch, FakeJev(JevError("skip")), FakeLLM("garbage"))
    poller = _trier_poller(world, BridgeStore(path), trier)

    poller.poll(now=datetime(2026, 10, 22, 12, 1, tzinfo=UTC))  # init
    _add_row(
        path,
        _bridge_row(
            "m2", datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
            content="רונה צריכה להביא כובע מחר",
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 13, 5, tzinfo=UTC))  # conversation 1: kid_named

    world.session.execute(sql_text("DELETE FROM entity_aliases"))
    world.session.flush()
    _add_row(
        path,
        _bridge_row(
            "m3", datetime(2026, 10, 22, 16, 0, tzinfo=UTC),
            content="רונה ואיתמר חוזרים באוטובוס",
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 16, 5, tzinfo=UTC))  # conversation 2

    world.session.expire_all()
    items = (
        world.session.query(MemoryItem)
        .order_by(MemoryItem.occurred_at.asc())
        .all()
    )
    assert items[0].triage_method == "kid_named", "precondition: the alias matched"
    assert items[1].triage_method != "kid_named", (
        "after the alias row was deleted, the refreshed kid names must not "
        "match רונה anymore (replace, not append)"
    )


def test_refresh_failure_savepoint_keeps_pending_captures_and_pass(
    triage_world: Any, tmp_path: Any, caplog: Any
) -> None:
    """A refresh that RAISES (DB hiccup) must: leave the pass running on the
    last-known kid names, log one warning, record an ops event, roll back
    ONLY the savepoint (pending captures of earlier passes survive), and
    leave the session usable for the rest of the pass and the next one."""
    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        CHAT_A_JID,
        _add_row,
        _bridge_row,
        _write_bridge,
    )

    from brain.db.models import EntityAlias  # noqa: PLC0415
    from brain.services.triage import RefreshableTrier  # noqa: PLC0415
    from brain.services.whatsapp import BridgeStore  # noqa: PLC0415

    class _PoisonedTrier(RefreshableTrier):
        """Real savepoint exercise: the poisoned read executes deliberately
        broken SQL on the shared session, so the abort happens inside the
        savepoint on a real database, not in a mock."""

        fail_next = False

        def _read_kid_names(self) -> list[str]:
            if self.fail_next:
                self._session.execute(sql_text("SELECT * FROM __no_such_table_xyz__"))
            return super()._read_kid_names()

    world = triage_world
    itamar = _confirmed_kid(world.memory, "Itamar")
    # Hebrew alias seeded UP FRONT: the kid override matches איתמר from the
    # very first pass, so the failure test isolates the refresh behaviour.
    world.memory_repo.add_alias(
        EntityAlias(entity_id=itamar.id, alias="איתמר", lang="he")
    )
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("m1", datetime(2026, 10, 22, 12, 0, tzinfo=UTC), content="שלום")],
    )
    trier = _PoisonedTrier(world.session, jev=FakeJev(JevError("skip")), llm=FakeLLM("garbage"))
    poller = _trier_poller(world, BridgeStore(path), trier)
    poller.poll(now=datetime(2026, 10, 22, 12, 1, tzinfo=UTC))  # init

    _add_row(
        path,
        _bridge_row(
            "m2", datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
            content="מה צריך לאיתמר מחר?",
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 13, 5, tzinfo=UTC))  # conversation 1: kid_named

    trier.fail_next = True
    _add_row(
        path,
        _bridge_row(
            "m3", datetime(2026, 10, 22, 16, 0, tzinfo=UTC),
            content="איתמר לא שכח את הכובע?",
        ),
    )
    with caplog.at_level("WARNING"):
        poller.poll(now=datetime(2026, 10, 22, 16, 5, tzinfo=UTC))  # refresh fails; pass continues
    assert any("refresh failed" in rec.message for rec in caplog.records), (
        "one warning must be logged on the failed refresh"
    )
    with engine.begin() as conn:
        ops_count = conn.execute(
            sql_text(
                "SELECT count(*) FROM ops_events "
                "WHERE component = 'triage' AND kind = 'refresh_failed'"
            )
        ).scalar_one()
    assert ops_count == 1, "the failed refresh is recorded as an ops event"

    world.session.expire_all()
    items = (
        world.session.query(MemoryItem).order_by(MemoryItem.occurred_at.asc()).all()
    )
    assert len(items) == 2, (
        "the conversation captured BEFORE the failed refresh must survive "
        "(savepoint rollback only — never a session rollback)"
    )
    assert items[0].triage_method == "kid_named"
    assert items[1].triage_method == "kid_named", (
        "the pass carries on with the LAST-KNOWN kid names — Itamar still matches"
    )

    trier.fail_next = False
    _add_row(
        path,
        _bridge_row(
            "m4", datetime(2026, 10, 22, 19, 0, tzinfo=UTC),
            content="איתמר הגיע הביתה",
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 19, 5, tzinfo=UTC))  # session usable, refresh works
    world.session.expire_all()
    assert world.session.query(MemoryItem).count() == 3


def test_fresh_trier_instance_honours_aliases_without_refresh(
    triage_world: Any,
) -> None:
    """The backfill CLI builds a FRESH trier per invocation: aliases already
    in memory are honoured with zero refresh() calls."""
    from brain.db.models import EntityAlias  # noqa: PLC0415
    from brain.services.triage import RefreshableTrier  # noqa: PLC0415

    world = triage_world
    rona = _confirmed_kid(world.memory, "Rona")
    world.memory_repo.add_alias(EntityAlias(entity_id=rona.id, alias="רונה", lang="he"))
    trier = RefreshableTrier(world.session, jev=FakeJev(JevError("skip")), llm=FakeLLM("garbage"))
    item = _conversation("[WhatsApp · Chat A]\n[21/10/2026, 18:23] מעין: מה צריך לרונה?")
    result = trier(item)
    assert result is not None and result.method == "kid_named"
    assert trier.refresh_calls == 0, "a fresh trier needs no refresh call"


def test_build_trier_is_wired_refreshable(triage_world: Any, monkeypatch: Any) -> None:
    """cli._build_trier returns the refreshable trier the poller hook expects."""
    trier = _wired_trier(triage_world, monkeypatch, FakeJev(JevError("skip")), FakeLLM("garbage"))
    assert callable(trier)
    assert callable(getattr(trier, "refresh", None)), (
        "_build_trier must return a refreshable trier — poll() calls refresh() once per pass"
    )


# --- startup tolerance (worker_3's post-merge review of #111) --------------------
# RefreshableTrier.__init__ must NOT read kid names eagerly-to-death: a DB
# hiccup at poller startup used to kill the WhatsApp agent. Construction now
# survives a failed first read (warning + ops event, NO kid names — the
# deterministic override stays off until the first successful refresh()), and
# the backfill CLI refuses a one-shot run on that state (exit non-zero: with no
# kid names it would silently miss kid mentions).


def _break_kid_names_read(monkeypatch: Any, state: dict) -> None:
    """Make MemoryService.overview raise while state['broken'] is True."""
    from brain.services.memory import MemoryService  # noqa: PLC0415

    real_overview = MemoryService.overview

    def flaky_overview(self: Any, **kwargs: Any) -> Any:
        if state["broken"]:
            raise RuntimeError("db hiccup")
        return real_overview(self, **kwargs)

    monkeypatch.setattr(MemoryService, "overview", flaky_overview)


def test_trier_construction_survives_failed_kid_names_read(
    triage_world: Any, tmp_path: Any, monkeypatch: Any, caplog: Any
) -> None:
    """RED-first: the first kid-names read raising must not kill construction.
    The trier starts with NO kid names (override inert, triage still runs via
    jev/llm), logs one warning + one ops event, and the next pass after the DB
    recovers fills the names (kid_named fires again)."""
    import logging  # noqa: PLC0415

    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        CHAT_A_JID,
        _add_row,
        _bridge_row,
        _write_bridge,
    )

    from brain.services.whatsapp import BridgeStore  # noqa: PLC0415

    world = triage_world
    rona = _confirmed_kid(world.memory, "Rona")
    from brain.db.models import EntityAlias  # noqa: PLC0415

    world.memory_repo.add_alias(EntityAlias(entity_id=rona.id, alias="רונה", lang="he"))
    state = {"broken": True}
    _break_kid_names_read(monkeypatch, state)

    with caplog.at_level(logging.WARNING, logger="brain.services.triage"):
        # Construction itself hits the broken read — must survive.
        trier = _wired_trier(world, monkeypatch, FakeJev(JevError("skip")), FakeLLM(
            '{"label": "noise", "confidence": 0.8}'
        ))
    assert trier._kid_names == [], "a failed first read must start with no kid names"
    assert trier.kid_names_loaded is False
    assert any(
        "kid-names" in rec.message.lower() for rec in caplog.records
    ), caplog.text
    ops_rows = world.session.execute(
        sql_text(
            "SELECT count(*) FROM ops_events "
            "WHERE component='triage' AND kind='kid_names_unavailable'"
        )
    ).scalar()
    assert ops_rows == 1, "construction must record exactly one ops event"

    # Pass 1: the first poll only initializes the cursor at the live edge.
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("m1", datetime(2026, 10, 22, 12, 0, tzinfo=UTC), content="שלום")],
    )
    poller = _trier_poller(world, BridgeStore(path), trier)
    poller.poll(now=datetime(2026, 10, 22, 12, 1, tzinfo=UTC))

    # Pass 2 (read still broken): the conversation NAMES the kid, but with no
    # kid names the override must stay OFF — triage falls through to jev/llm.
    _add_row(
        path,
        _bridge_row(
            "m2", datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
            content="רונה צריכה להביא כובע מחר",
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 13, 5, tzinfo=UTC))

    world.session.expire_all()
    items = world.session.query(MemoryItem).order_by(MemoryItem.occurred_at.asc()).all()
    got = [(i.triage_method, i.raw_text[:60]) for i in items]
    assert len(items) == 1, f"expected 1 conversation, got {got}"
    assert items[0].triage_method == "llm", (
        "with no kid names (failed startup read) the override must not fire — "
        "the conversation is still triaged, via the model fallback"
    )

    # Pass 3 (DB recovered): refresh() fills the names — kid_named fires.
    state["broken"] = False
    _add_row(
        path,
        _bridge_row(
            "m3", datetime(2026, 10, 22, 16, 0, tzinfo=UTC),
            content="מה צריך לארוז לרונה ליום ראשון?",
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 16, 5, tzinfo=UTC))

    world.session.expire_all()
    items = world.session.query(MemoryItem).order_by(MemoryItem.occurred_at.asc()).all()
    assert [i.triage_method for i in items] == ["llm", "kid_named"], (
        "after the DB recovers, the next pass's refresh() must restore the "
        "kid_named override"
    )


def test_backfill_refuses_to_run_when_kid_names_read_fails(
    triage_world: Any, monkeypatch: Any, capsys: Any
) -> None:
    """RED-first: the backfill CLI on a failed first kid-names read must exit
    NON-ZERO with a clear message (a one-shot pass on [] names would silently
    miss kid mentions), triaging nothing and calling no model."""
    from brain import cli  # noqa: PLC0415

    world = triage_world
    _confirmed_kid(world.memory, "Rona")
    item = world.items.add(_conversation(
        "[WhatsApp · Chat A]\n[21/10/2026, 18:23] מעין: מה צריך לרונה?"
    ))
    world.session.commit()

    state = {"broken": True}
    _break_kid_names_read(monkeypatch, state)
    jev = FakeJev(JevChoice(option="relevant", confidence=0.97))
    llm = FakeLLM('{"label": "relevant", "confidence": 0.9}')
    monkeypatch.setattr("brain.api.deps.get_jev_client", lambda: jev)
    monkeypatch.setattr("brain.api.deps.get_llm_client", lambda: llm)

    rc = cli.main(["whatsapp-triage", "--limit", "100"])

    assert rc != 0, "a one-shot backfill must not run on a failed kid-names read"
    err = capsys.readouterr().err
    assert "kid-names" in err.lower(), err
    world.session.expire_all()
    assert world.items.get(item.id).triage_label is None, "nothing may be triaged"
    assert jev.calls == 0 and llm.prompts == [], "no model calls on refusal"


# --- media-only conversations (step D) ------------------------------------------
# A conversation whose every message is an UNREAD media marker has nothing
# readable to triage: labelling a filename is how media-only conversations
# landed NOISE on zero content. Skip while unread (both the poller AND the
# backfill path — the backfill calls the trier directly), and re-triage ONCE
# when the last marker is replaced, never touching user-acted items.


class _CountingTrier:
    """Delegates to a real/scripted trier and counts invocations."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def __call__(self, item: Any) -> Any:
        self.calls += 1
        return self._inner(item)


_MEDIA_ONLY_TEXT = (
    "[WhatsApp · Chat A]\n"
    "[22/10/2026, 13:00] Rinat: [image: photo.jpg — attachment not read yet]"
)


def _media_poller_for_triage(world: Any, bridge: Any, trier: Any) -> Any:
    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        DEFAULT_GAP_MINUTES as INGEST_GAP,
    )
    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        ZONE,
        FakeIndexer,
    )

    from brain.services.whatsapp import WhatsAppPoller  # noqa: PLC0415

    return WhatsAppPoller(
        bridge=bridge,
        state=world.state,
        items=world.items,
        memory=world.memory_repo,
        indexer=FakeIndexer(),
        trier=trier,
        zone_name=ZONE,
        gap_minutes=INGEST_GAP,
        commit=False,
    )


def _captured_media_only_conversation(
    triage_world: Any, tmp_path: Any, trier: Any, *, filename: str = "photo.jpg"
) -> Any:
    """One captured conversation whose only message is an unread image."""
    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        CHAT_A_JID,
        _add_row,
        _bridge_row,
        _write_bridge,
    )

    from brain.services.whatsapp import BridgeStore  # noqa: PLC0415

    world = triage_world
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("m1", datetime(2026, 10, 22, 12, 0, tzinfo=UTC), content="שלום")],
    )
    poller = _media_poller_for_triage(world, BridgeStore(path), trier)
    poller.poll(now=datetime(2026, 10, 22, 12, 1, tzinfo=UTC))  # init at the live edge
    _add_row(
        path,
        _bridge_row(
            "m2",
            datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
            content="",  # caption-less: nothing readable but the marker
            media_type="image",
            filename=filename,
            url="https://mmg.whatsapp.net/v/t62.7118-24/secret.enc",
            media_key=b"k" * 8,
            file_length=2048,
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 13, 5, tzinfo=UTC))  # captures the media-only conv
    world.session.expire_all()
    return world, poller, world.session.query(MemoryItem).one()


def test_media_only_conversation_is_skipped_while_unread(
    triage_world: Any, tmp_path: Any
) -> None:
    """(a) An unread-media-only conversation is captured but NOT triaged:
    no trier call, triage columns stay NULL."""
    trier = _CountingTrier(lambda item: TriageResult("noise", 0.97, "jev", TRIAGE_VERSION))
    world, poller, item = _captured_media_only_conversation(triage_world, tmp_path, trier)
    assert "attachment not read yet" in item.raw_text
    assert trier.calls == 0, "nothing readable: the trier must not run"
    assert item.triage_label is None and item.triage_method is None


def test_media_only_conversation_retriages_after_the_last_marker_is_replaced(
    triage_world: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """(b) When the media is read and the last marker is replaced, the
    conversation is re-triaged through the normal update path."""
    import uuid as uuid_mod  # noqa: PLC0415

    from brain.db.models import EntityAlias  # noqa: PLC0415
    from brain.services.whatsapp import BridgeMessage  # noqa: PLC0415

    world = triage_world
    rona = _confirmed_kid(world.memory, "Rona")
    world.memory_repo.add_alias(
        EntityAlias(entity_id=rona.id, alias="רונה", lang="he")
    )
    # the wired trier is built AFTER the alias exists (fresh read at build)
    wired = _wired_trier(world, monkeypatch, FakeJev(JevError("skip")), FakeLLM("garbage"))
    trier = _CountingTrier(wired)

    # the unread marker already contains the filename (nothing else readable);
    # the SKIP is what keeps it unlabelled until the media is read.
    world2, poller, item = _captured_media_only_conversation(
        world, tmp_path, trier, filename="רונה כובע.jpg"
    )
    assert trier.calls == 0

    # the media is read: the marker is replaced by the attachment link.
    # The filename carries the kid's name, so the fresh re-triage resolves
    # kid_named from the (now readable) text.
    message = BridgeMessage(
        id="m2",
        chat_jid="jid-a@g.us",
        sender="Rinat",
        content="",
        timestamp_utc=datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
        is_from_me=False,
        media_type="image",
        filename="רונה כובע.jpg",
        file_length=2048,
    )
    poller._replace_media_marker(item.id, message, uuid_mod.uuid4())
    # commit=False in these tests defers persistence to the poll's single
    # commit point; flush here so the assertions read the written state
    # (SessionLocal is autoflush=False, and expire_all would otherwise
    # discard the pending in-memory columns).
    world.session.flush()
    world.session.expire_all()
    item2 = world.session.query(MemoryItem).one()
    assert trier.calls == 1, "the re-triage fires once, when the last marker is replaced"
    assert item2.triage_method == "kid_named"
    assert item2.triage_label == "relevant"


def test_mixed_conversation_is_triaged_at_capture(triage_world: Any, tmp_path: Any) -> None:
    """(c) A captioned image is a MIXED conversation: triaged at capture as usual."""
    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        CHAT_A_JID,
        _add_row,
        _bridge_row,
        _write_bridge,
    )

    from brain.services.whatsapp import BridgeStore  # noqa: PLC0415

    world = triage_world
    trier = _CountingTrier(lambda item: TriageResult("relevant", 0.95, "jev", TRIAGE_VERSION))
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("m1", datetime(2026, 10, 22, 12, 0, tzinfo=UTC), content="שלום")],
    )
    poller = _media_poller_for_triage(world, BridgeStore(path), trier)
    poller.poll(now=datetime(2026, 10, 22, 12, 1, tzinfo=UTC))
    _add_row(
        path,
        _bridge_row(
            "m2",
            datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
            content="איזה כיף!!",
            media_type="image",
            filename="photo.jpg",
            url="https://mmg.whatsapp.net/v/t62.7118-24/secret.enc",
            media_key=b"k" * 8,
            file_length=2048,
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 13, 5, tzinfo=UTC))
    world.session.expire_all()
    item = world.session.query(MemoryItem).one()
    assert trier.calls == 1, "a caption makes the conversation readable at capture"
    assert item.triage_label == "relevant"


def test_partially_read_conversation_stays_skipped(triage_world: Any, tmp_path: Any) -> None:
    """(d) Two unread markers, one read: still nothing fully readable -> skip."""
    import uuid as uuid_mod  # noqa: PLC0415

    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        CHAT_A_JID,
        _add_row,
        _bridge_row,
        _write_bridge,
    )

    from brain.services.whatsapp import BridgeMessage, BridgeStore  # noqa: PLC0415

    world = triage_world
    trier = _CountingTrier(lambda item: TriageResult("noise", 0.97, "jev", TRIAGE_VERSION))
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("m1", datetime(2026, 10, 22, 12, 0, tzinfo=UTC), content="שלום")],
    )
    poller = _media_poller_for_triage(world, BridgeStore(path), trier)
    poller.poll(now=datetime(2026, 10, 22, 12, 1, tzinfo=UTC))
    _add_row(
        path,
        _bridge_row(
            "m2",
            datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
            content="",
            media_type="image",
            filename="a.jpg",
            url="https://mmg.whatsapp.net/v/t62.7118-24/a.enc",
            media_key=b"k" * 8,
            file_length=2048,
        ),
    )
    _add_row(
        path,
        _bridge_row(
            "m3",
            datetime(2026, 10, 22, 13, 1, tzinfo=UTC),
            content="",
            media_type="image",
            filename="b.jpg",
            url="https://mmg.whatsapp.net/v/t62.7118-24/b.enc",
            media_key=b"k" * 8,
            file_length=2048,
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 13, 5, tzinfo=UTC))
    world.session.expire_all()
    item = world.session.query(MemoryItem).one()
    assert trier.calls == 0
    assert item.raw_text.count("attachment not read yet") == 2

    message = BridgeMessage(
        id="m2",
        chat_jid="jid-a@g.us",
        sender="Rinat",
        content="",
        timestamp_utc=datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
        is_from_me=False,
        media_type="image",
        filename="a.jpg",
        file_length=2048,
    )
    poller._replace_media_marker(item.id, message, uuid_mod.uuid4())
    world.session.expire_all()
    assert trier.calls == 0, "one marker still unread: the skip holds"
    item2 = world.session.query(MemoryItem).one()
    assert item2.triage_label is None


def test_retriage_after_read_never_downgrades_relevant(
    triage_world: Any, tmp_path: Any
) -> None:
    """(e) v3 guard through the post-read re-triage: a relevant conversation
    stays relevant even when the fresh triage says noise (blocked, recorded)."""
    import uuid as uuid_mod  # noqa: PLC0415

    from tests.services.test_whatsapp_ingest import (  # noqa: PLC0415
        CHAT_A_JID,  # noqa: PLC0415
        _add_row,
        _bridge_row,
        _write_bridge,
    )

    from brain.services.whatsapp import (
        BridgeMessage,  # noqa: PLC0415
        BridgeStore,  # noqa: PLC0415
    )

    world = triage_world
    # mixed conversation (caption -> readable at capture), media read later
    scripted = _CountingTrier(lambda item: iter_results.pop(0))
    iter_results = [
        TriageResult("relevant", 1.0, "kid_named", TRIAGE_VERSION),
        TriageResult("noise", 0.97, "jev", TRIAGE_VERSION),
    ]
    path = tmp_path / "messages.db"
    _write_bridge(
        path,
        [(CHAT_A_JID, "Chat A")],
        [_bridge_row("m1", datetime(2026, 10, 22, 12, 0, tzinfo=UTC), content="שלום")],
    )
    poller = _media_poller_for_triage(world, BridgeStore(path), scripted)
    poller.poll(now=datetime(2026, 10, 22, 12, 1, tzinfo=UTC))
    _add_row(
        path,
        _bridge_row(
            "m2",
            datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
            content="מה צריך לארוז לרונה?",
            media_type="image",
            filename="photo.jpg",
            url="https://mmg.whatsapp.net/v/t62.7118-24/secret.enc",
            media_key=b"k" * 8,
            file_length=2048,
        ),
    )
    poller.poll(now=datetime(2026, 10, 22, 13, 5, tzinfo=UTC))
    world.session.expire_all()
    item = world.session.query(MemoryItem).one()
    assert item.triage_label == "relevant" and scripted.calls == 1

    message = BridgeMessage(
        id="m2",
        chat_jid="jid-a@g.us",
        sender="Rinat",
        content="מה צריך לארוז לרונה?",
        timestamp_utc=datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
        is_from_me=False,
        media_type="image",
        filename="photo.jpg",
        file_length=2048,
    )
    poller._replace_media_marker(item.id, message, uuid_mod.uuid4())
    world.session.expire_all()
    item2 = world.session.query(MemoryItem).one()
    assert scripted.calls == 2, "the re-triage ran after the read"
    assert item2.triage_label == "relevant", (
        "v3: relevant may never become noise, even through the post-read re-triage"
    )
    assert item2.triage_method == "kid_named"


def test_backfill_skips_unread_media_only_items(monkeypatch: Any, capsys: Any) -> None:
    """(f) The backfill command calls the trier DIRECTLY: the media-only skip
    must hold there too — 0 trier/model calls, columns stay NULL, and the
    summary counts it as skipped (unread media), not noise/relevant."""
    from brain import cli

    _wipe_items()
    session = SessionLocal()
    repo = ItemsRepository(session)
    media_only = repo.add(_conversation(_MEDIA_ONLY_TEXT))
    session.commit()

    calls: list[MemoryItem] = []

    def fake_trier(item: MemoryItem) -> TriageResult | None:
        calls.append(item)
        return TriageResult("noise", 0.97, "jev", TRIAGE_VERSION)

    monkeypatch.setattr("brain.cli._build_trier", lambda session: fake_trier)
    rc = cli.main(["whatsapp-triage", "--limit", "100"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "skipped (unread media) 1" in out, out
    assert calls == [], "the trier must not even run for an unread-media-only item"
    session.expire_all()
    item = repo.get(media_only.id)
    assert item.triage_label is None and item.triage_method is None
    session.close()


def test_post_read_retriage_never_touches_user_acted_items(
    triage_world: Any, tmp_path: Any
) -> None:
    """(g) The user acted on the item (filed it) while it was still unread
    media: the post-read re-triage must not touch it."""
    import uuid as uuid_mod  # noqa: PLC0415

    from brain.db.enums import ItemStatus  # noqa: PLC0415
    from brain.services.whatsapp import (
        BridgeMessage,  # noqa: PLC0415
        )

    world = triage_world
    trier = _CountingTrier(lambda item: TriageResult("noise", 0.97, "jev", TRIAGE_VERSION))
    world2, poller, item = _captured_media_only_conversation(triage_world, tmp_path, trier)
    assert trier.calls == 0

    item.status = ItemStatus.FILED  # the user filed it while it was unread media
    world.items.commit()
    world.session.expire_all()
    item = world.session.query(MemoryItem).one()

    message = BridgeMessage(
        id="m2",
        chat_jid="jid-a@g.us",
        sender="Rinat",
        content="",
        timestamp_utc=datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
        is_from_me=False,
        media_type="image",
        filename="photo.jpg",
        file_length=2048,
    )
    poller._replace_media_marker(item.id, message, uuid_mod.uuid4())
    world.session.expire_all()
    item2 = world.session.query(MemoryItem).one()
    assert trier.calls == 0, "user-acted items are never re-triaged"
    assert item2.triage_label is None
