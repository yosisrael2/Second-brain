"""PR C: proposal cards for extracted children (delta items 19, 20, D2, D4, D7).

Offline: DB-backed tests run the REAL EnrichmentService with the STUB
extractor on the *_test database; bot-level tests use fakes. No Telegram
messages, no live writes, no real names (fixtures use 'Dana'/generic).
"""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from brain.db.models import MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.repositories.memory import MemoryRepository
from brain.services.extraction.service import EnrichmentService
from brain.services.items import ItemsService
from brain.services.memory import MemoryService
from brain.services.proposals import (
    CB_PROPOSED_DROP,
    CB_PROPOSED_FIX,
    CB_PROPOSED_KEEP,
    CB_PROPOSED_KEEP_ALL,
    apply_drop,
    apply_fix,
    apply_keep,
    child_line,
    due_proposed_children,
    mark_pinged,
    proposal_keyboard,
    render_source_card,
    source_short_title,
    superseded_marker,
)
from conftest import assert_test_database

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
TZ = "Asia/Jerusalem"


class _Wipe:
    @staticmethod
    def wipe(session: Session) -> None:
        session.execute(text("DELETE FROM enrichment_jobs"))
        session.execute(text("DELETE FROM item_sources"))
        session.execute(text("DELETE FROM memory_items"))
        session.execute(text("DELETE FROM memory_facts"))
        session.execute(text("DELETE FROM entity_aliases"))
        session.execute(text("DELETE FROM entities"))
        session.commit()


@pytest.fixture(name="world")
def world_fixture(database: None):
    """Fresh DB world (same pattern as the extraction tests)."""
    assert_test_database(str(engine.url))
    session = SessionLocal()
    _Wipe.wipe(session)
    repo = ItemsRepository(session)
    memory = MemoryService(MemoryRepository(session))
    yield repo, session, memory
    session.rollback()
    session.close()
    _Wipe.wipe(session)


def _capture(repo: ItemsRepository, body: str, source: str = "email") -> MemoryItem:
    item = repo.add(MemoryItem(raw_text=body, source=source))
    repo.commit()  # the bot's proposal sessions are separate transactions
    return item


SOURCE_BODY = "שלום,\nמחר כינוס הורים באמפי 22.10\nחולצה לבנה ליום חמישי 23.10\n"


class Recorder:
    def __init__(self) -> None:
        self.outcomes: list = []

    def __call__(self, outcomes) -> None:
        self.outcomes.extend(outcomes)


def _service(repo, session, memory, hook=None, extractor=None) -> EnrichmentService:
    return EnrichmentService(
        repo, session, memory=memory, on_children_changed=hook, extractor=extractor
    )


# --- rendering -----------------------------------------------------------------


class TestSourceTitle:
    def test_prefers_the_subject_from_meta(self, world):
        repo, _session, _memory = world
        source = _capture(repo, SOURCE_BODY)
        source.meta = {"subject": "Talan weekly", "from": "t@example.com"}
        assert source_short_title(source) == "Talan weekly"

    def test_falls_back_to_the_first_line(self, world):
        repo, _session, _memory = world
        source = _capture(repo, SOURCE_BODY)
        assert source_short_title(source).startswith("שלום")


class TestCardRendering:
    def test_header_counts_children_and_names_the_source(self, world):
        repo, session, memory = world
        source = _capture(repo, SOURCE_BODY)
        service = _service(repo, session, memory)
        service.enqueue(source)
        service.run_once(NOW)
        children = session.query(MemoryItem).filter(MemoryItem.child_state == "proposed").all()
        card = render_source_card(source, children, TZ)
        assert card.startswith(f"🧠 found {len(children)} items in")
        for child in children:
            assert child_line(child, TZ) in card

    def test_child_line_kind_emoji_date_applies_to(self, world):
        repo, session, _memory = world
        child = repo.add(
            MemoryItem(
                raw_text="Assembly on Sunday",
                source="email",
                type="event",
                occurred_at=datetime(2026, 10, 25, 9, 15, tzinfo=UTC),
                child_state="proposed",
                applies_to=["Dana"],
            )
        )
        line = child_line(child, TZ)
        assert line.startswith("📅")  # event
        assert "Assembly on Sunday" in line
        assert "25.10" in line  # resolved date in LOCAL time (25.10 12:15 IDT)
        assert "Dana" in line

    def test_conflict_child_carries_the_warning(self, world):
        repo, _session, _memory = world
        child = repo.add(
            MemoryItem(
                raw_text="Tuesday trip 7.10",
                source="email",
                type="event",
                child_state="proposed",
                date_conflict=True,
            )
        )
        assert child_line(child, TZ).startswith("⚠️")

    def test_task_and_note_emojis(self, world):
        repo, _session, _memory = world
        task = repo.add(
            MemoryItem(
                raw_text="Send the shirt", source="email", type="task", child_state="proposed"
            )
        )
        note = repo.add(
            MemoryItem(
                raw_text="Reading book every day",
                source="email",
                type="note",
                child_state="proposed",
            )
        )
        assert child_line(task, TZ).startswith("✔️")
        assert child_line(note, TZ).startswith("ℹ️")

    def test_escaping_is_applied(self, world):
        repo, _session, _memory = world
        child = repo.add(
            MemoryItem(
                raw_text="Gift <b>for</b> Dana & me",
                source="email",
                type="task",
                child_state="proposed",
            )
        )
        line = child_line(child, TZ)
        assert "<b>" not in line
        assert html.escape("Gift <b>for</b> Dana & me") in line


class TestSupersedeHook:
    """D4 (item 20): a superseded child renders struck-through; the relation
    logic itself is PR B — only the rendering hook exists now."""

    def test_superseded_child_gets_the_struck_marker(self, world):
        repo, _session, _memory = world
        child = repo.add(
            MemoryItem(
                raw_text="Old date Tuesday 7.10",
                source="email",
                type="event",
                child_state="kept",
                extraction_meta={"superseded_by": str(uuid.uuid4())},
            )
        )
        assert superseded_marker(child) != ""
        assert "↩︎" in child_line(child, TZ)

    def test_normal_child_has_no_marker(self, world):
        repo, _session, _memory = world
        child = repo.add(
            MemoryItem(raw_text="Fresh child", source="email", type="task", child_state="proposed")
        )
        assert superseded_marker(child) == ""


# --- keyboard + callback size ----------------------------------------------------


class TestKeyboard:
    def test_rows_per_child_plus_keep_all(self, world):
        repo, session, _memory = world
        children = [
            repo.add(
                MemoryItem(
                    raw_text=f"child {i}", source="email", type="task", child_state="proposed"
                )
            )
            for i in range(3)
        ]
        token = "abcd1234"
        rows = proposal_keyboard(token, children)
        flat = [data for row in rows for _label, data in row]
        assert sum(1 for d in flat if d.startswith(f"{CB_PROPOSED_KEEP}:")) == 3
        assert sum(1 for d in flat if d.startswith(f"{CB_PROPOSED_FIX}:")) == 3
        assert sum(1 for d in flat if d.startswith(f"{CB_PROPOSED_DROP}:")) == 3
        assert any(d.startswith(f"{CB_PROPOSED_KEEP_ALL}:") for d in flat)

    def test_callback_data_stays_under_64_bytes(self, world):
        repo, session, _memory = world
        children = [
            repo.add(
                MemoryItem(
                    raw_text=f"child {i}", source="email", type="task", child_state="proposed"
                )
            )
            for i in range(3)
        ]
        rows = proposal_keyboard("abcdef12", children)
        for row in rows:
            for _label, data in row:
                assert len(data.encode()) < 64


# --- actions -----------------------------------------------------------------


class TestActions:
    def test_keep_marks_kept(self, world):
        repo, _session, _memory = world
        child = repo.add(
            MemoryItem(raw_text="child", source="email", type="task", child_state="proposed")
        )
        apply_keep(repo, child)
        assert child.child_state == "kept"

    def test_drop_marks_dropped(self, world):
        repo, _session, _memory = world
        child = repo.add(
            MemoryItem(raw_text="child", source="email", type="task", child_state="proposed")
        )
        apply_drop(repo, child)
        assert child.child_state == "dropped"

    def test_dropped_child_stays_suppressed_after_a_re_run(self, world):
        """Item 30 + 18: run twice; the dropped child is never re-proposed
        and never duplicated."""
        repo, session, memory = world
        source = _capture(repo, SOURCE_BODY)
        service = _service(repo, session, memory)
        service.enqueue(source)
        service.run_once(NOW)
        children = session.query(MemoryItem).filter(MemoryItem.child_state == "proposed").all()
        assert len(children) == 2
        apply_drop(repo, children[0])
        # re-run the SAME source on the backfill budget (the daily job is done)
        again = _service(repo, session, memory)
        again.enqueue(source, budget="backfill")
        report = again.run_once(NOW, backfill=True)
        ids = [row.id for row in session.query(MemoryItem).all()]
        assert len(ids) == 3  # source + 2 children: no duplicate was created
        session.expire_all()
        dropped = session.get(MemoryItem, children[0].id)
        assert dropped.child_state == "dropped"  # still dropped, never resurrected
        assert report.children_created == 0

    def test_fix_revalidates_through_the_date_rules(self, world):
        repo, session, memory = world
        source = _capture(repo, SOURCE_BODY)
        source.occurred_at = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)  # sent Tue 20.10
        child = repo.add(
            MemoryItem(
                raw_text="Trip (wrong title)",
                source="email",
                type="event",
                child_state="proposed",
                occurred_at=None,
            )
        )
        fixed = apply_fix(
            repo,
            child,
            title="Trip to Eshkolot",
            when_raw="יום חמישי 29.10",
            source=source,
            tz_name=TZ,
        )
        session.refresh(fixed)
        assert fixed.raw_text == "Trip to Eshkolot"
        assert fixed.occurred_at is not None
        from zoneinfo import ZoneInfo

        local = fixed.occurred_at.astimezone(ZoneInfo(TZ))
        assert local.strftime("%d.%m") == "29.10"  # resolved in the CONFIGURED zone
        assert fixed.child_state == "kept"  # user acted (item 18: pipeline never overwrites)
        assert fixed.extraction_meta.get("user_edited") is True

    def test_fix_with_contradictory_weekday_flags_the_conflict(self, world):
        repo, session, memory = world
        source = _capture(repo, SOURCE_BODY)
        source.occurred_at = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
        child = repo.add(
            MemoryItem(raw_text="Trip", source="email", type="event", child_state="proposed")
        )
        # 7.10.2026 is a WEDNESDAY: "יום שלישי 7.10" (Tuesday 7.10) contradicts
        fixed = apply_fix(
            repo,
            child,
            title="Trip",
            when_raw="יום שלישי 7.10",
            source=source,
            tz_name=TZ,
        )
        session.refresh(fixed)
        assert fixed.date_conflict is True
        assert fixed.status == "needs_review"

    def test_fix_rejects_an_empty_title(self, world):
        repo, _session, _memory = world
        child = repo.add(
            MemoryItem(raw_text="child", source="email", type="task", child_state="proposed")
        )
        with pytest.raises(ValueError):
            apply_fix(repo, child, title="   ", when_raw="", source=None, tz_name=TZ)

    def test_fix_all_keeps_proposed_children_of_the_source(self, world):
        """'Keep all' on a source card keeps every still-proposed child."""
        repo, session, memory = world
        source = _capture(repo, SOURCE_BODY)
        service = _service(repo, session, memory)
        service.enqueue(source)
        service.run_once(NOW)
        proposed = session.query(MemoryItem).filter(MemoryItem.child_state == "proposed").all()
        for child in proposed:
            apply_keep(repo, child)
        session.expire_all()
        kept = session.query(MemoryItem).filter(MemoryItem.child_state == "kept").count()
        assert kept == len(proposed)


# --- the 48h ping (D7) -------------------------------------------------------------


class TestDuePings:
    def _task(self, repo, when=None, state="proposed", type_="task"):
        return repo.add(
            MemoryItem(
                raw_text="due soon task",
                source="email",
                type=type_,
                occurred_at=when,
                child_state=state,
            )
        )

    def test_proposed_task_due_within_48h_is_due(self, world):
        repo, _session, _memory = world
        child = self._task(repo, when=NOW + timedelta(hours=24))
        due = due_proposed_children(_session_of(repo), NOW, TZ)
        assert [c.id for c in due] == [child.id]

    def test_kept_dropped_and_far_off_never_ping(self, world):
        repo, _session, _memory = world
        self._task(repo, when=NOW + timedelta(hours=24), state="kept")
        self._task(repo, when=NOW + timedelta(hours=24), state="dropped")
        self._task(repo, when=NOW + timedelta(hours=72))  # beyond the 48h window
        self._task(repo, when=None)  # dateless: never a ping
        self._task(repo, when=NOW + timedelta(hours=24), type_="event")  # not a task
        assert due_proposed_children(_session_of(repo), NOW, TZ) == []

    def test_reminders_due_within_48h_ping_too(self, world):
        repo, _session, _memory = world
        self._task(repo, when=NOW + timedelta(hours=24), type_="reminder")
        due = due_proposed_children(_session_of(repo), NOW, TZ)
        assert len(due) == 1
        assert due[0].type == "reminder"

    def test_ping_fires_once_then_never_again(self, world):
        repo, _session, _memory = world
        child = self._task(repo, when=NOW + timedelta(hours=24))
        session = _session_of(repo)
        first = due_proposed_children(session, NOW, TZ)
        assert len(first) == 1
        mark_pinged(first, NOW)
        assert due_proposed_children(session, NOW, TZ) == []
        assert (child.extraction_meta or {}).get("pinged_at") is not None


def _session_of(repo):  # the repo owns the session in these tests
    return repo._db  # noqa: SLF001 (tests reach the session the repo already has)


# --- the runner's completion hook ------------------------------------------------


class TestRunnerHook:
    def test_hook_receives_the_touched_sources(self, world):
        repo, session, memory = world
        source = _capture(repo, SOURCE_BODY)
        seen: list = []
        service = _service(repo, session, memory, hook=seen.extend)
        service.enqueue(source)
        service.run_once(NOW)
        assert len(seen) == 1
        outcome = seen[0]
        assert outcome.source.id == source.id
        assert outcome.created == 2
        assert outcome.updated == 0

    def test_quiet_runs_do_not_fire_the_hook(self, world):
        repo, session, memory = world
        source = _capture(repo, SOURCE_BODY)
        seen: list = []
        service = _service(repo, session, memory, hook=seen.extend)
        service.enqueue(source)
        service.run_once(NOW)
        seen.clear()
        # a second pass with nothing new: enqueue is idempotent, job already done
        service.enqueue(source)
        service.run_once(NOW)
        assert seen == []

    def test_a_raising_hook_never_breaks_the_run(self, world):
        repo, session, memory = world
        source = _capture(repo, SOURCE_BODY)

        def boom(_outcomes):
            raise RuntimeError("hook blew up")

        service = _service(repo, session, memory, hook=boom)
        service.enqueue(source)
        report = service.run_once(NOW)  # must not raise
        assert report.children_created == 2


# --- the listing rule (item 19 via D2) ---------------------------------------------


class TestListing:
    def test_proposed_children_are_unlisted_until_kept(self, world):
        repo, session, memory = world
        source = _capture(repo, SOURCE_BODY)
        service = _service(repo, session, memory)
        service.enqueue(source)
        service.run_once(NOW)
        service_items = ItemsService(repo)
        visible = list(
            service_items.list(item_type=None, project=None, status=None, limit=50, offset=0)
        )
        assert all(item.child_state is None for item in visible)  # children hidden
        proposed = session.query(MemoryItem).filter(MemoryItem.child_state == "proposed").all()
        for child in proposed:
            apply_keep(repo, child)
        visible = list(
            service_items.list(item_type=None, project=None, status=None, limit=50, offset=0)
        )
        kept_ids = {item.id for item in visible}
        for child in proposed:
            assert child.id in kept_ids  # kept children ARE listed


# --- bot-level flow (real DB, real core, fake adapter) ----------------------------


def _bot(world):
    """A TelegramBot core over the real test DB (factory yields real services)."""
    from contextlib import contextmanager
    from zoneinfo import ZoneInfo

    from brain.services.extraction.service import EnrichmentService
    from brain.telegram_bot import BotServices, TelegramBot

    repo, session, memory = world
    items = ItemsService(repo)
    service = EnrichmentService(repo, session, memory=memory)

    @contextmanager
    def factory():
        yield BotServices(ingest=None, items=items, search=None, ask=None, memory=memory)

    return TelegramBot(
        services_factory=factory,
        allowed_ids=frozenset({7}),
        timezone=ZoneInfo(TZ),
    ), service


class TestBotProposalFlow:
    def test_completion_hook_queues_one_card_per_allowed_chat(self, world):
        repo, session, memory = world
        bot, service = _bot(world)
        source = _capture(repo, SOURCE_BODY)
        service.on_children_changed = bot.proposals_changed
        service.enqueue(source)
        service.run_once(NOW)
        queued = bot.drain_outbound()
        assert [(c, r.text[:12]) for c, r in queued] == [(7, "🧠 found 2 it")]
        assert bot.drain_outbound() == []  # drained

    def test_keep_callback_round_trip(self, world):
        repo, session, memory = world
        bot, service = _bot(world)
        source = _capture(repo, SOURCE_BODY)
        service.on_children_changed = bot.proposals_changed
        service.enqueue(source)
        service.run_once(NOW)
        _chat, card = bot.drain_outbound()[0]
        token = _token_of(card, CB_PROPOSED_KEEP)
        reply = bot.handle_callback(f"{CB_PROPOSED_KEEP}:{token}:0", 7)
        session.expire_all()
        child = session.query(MemoryItem).filter(MemoryItem.child_state == "kept").one()
        assert child.raw_text  # state in the DB matches the tap
        assert "✅ Kept" in reply.text

    def test_drop_callback_suppresses_and_never_returns(self, world):
        repo, session, memory = world
        bot, service = _bot(world)
        source = _capture(repo, SOURCE_BODY)
        service.on_children_changed = bot.proposals_changed
        service.enqueue(source)
        service.run_once(NOW)
        _chat, card = bot.drain_outbound()[0]
        token = _token_of(card, CB_PROPOSED_DROP)
        bot.handle_callback(f"{CB_PROPOSED_DROP}:{token}:1", 7)
        # re-run the extraction: the dropped child must not come back
        again = EnrichmentService(repo, session, memory=memory)
        again.enqueue(source, budget="backfill")
        again.run_once(NOW, backfill=True)
        states = [row.child_state for row in session.query(MemoryItem).all()]
        assert states.count("dropped") == 1
        assert states.count("proposed") == 1  # the untouched one may update in place

    def test_keep_all_callback(self, world):
        repo, session, memory = world
        bot, service = _bot(world)
        source = _capture(repo, SOURCE_BODY)
        service.on_children_changed = bot.proposals_changed
        service.enqueue(source)
        service.run_once(NOW)
        _chat, card = bot.drain_outbound()[0]
        token = _token_of(card, CB_PROPOSED_KEEP_ALL)
        bot.handle_callback(f"{CB_PROPOSED_KEEP_ALL}:{token}", 7)
        session.expire_all()
        kept = session.query(MemoryItem).filter(MemoryItem.child_state == "kept").count()
        assert kept == 2

    def test_fix_flow_round_trip(self, world):
        repo, session, memory = world
        bot, service = _bot(world)
        source = _capture(repo, SOURCE_BODY)
        source.occurred_at = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
        repo.commit()
        service.on_children_changed = bot.proposals_changed
        service.enqueue(source)
        service.run_once(NOW)
        _chat, card = bot.drain_outbound()[0]
        token = _token_of(card, CB_PROPOSED_FIX)
        step1 = bot.handle_callback(f"{CB_PROPOSED_FIX}:{token}:0", 7)
        assert "New title" in step1.text
        step2 = bot.route("Trip to Eshkolot", 7)
        assert "Now the date" in step2.text
        step3 = bot.route("יום חמישי 29.10", 7)
        assert "Fixed" in step3.text
        session.expire_all()
        child = session.query(MemoryItem).filter(MemoryItem.child_state == "kept").one()
        assert child.raw_text == "Trip to Eshkolot"
        assert child.extraction_meta.get("user_edited") is True
        from zoneinfo import ZoneInfo

        assert child.occurred_at.astimezone(ZoneInfo(TZ)).strftime("%d.%m") == "29.10"

    def test_fix_cancel_aborts(self, world):
        repo, session, memory = world
        bot, service = _bot(world)
        source = _capture(repo, SOURCE_BODY)
        service.on_children_changed = bot.proposals_changed
        service.enqueue(source)
        service.run_once(NOW)
        _chat, card = bot.drain_outbound()[0]
        token = _token_of(card, CB_PROPOSED_FIX)
        bot.handle_callback(f"{CB_PROPOSED_FIX}:{token}:0", 7)
        reply = bot.route("cancel", 7)
        assert "cancelled" in reply.text
        session.expire_all()
        assert session.query(MemoryItem).filter(MemoryItem.child_state == "kept").count() == 0

    def test_card_text_is_escaped(self, world):
        repo, session, memory = world
        bot, service = _bot(world)
        source = _capture(repo, "Title <b>with</b> tags & stuff 22.10")
        service.on_children_changed = bot.proposals_changed
        service.enqueue(source)
        service.run_once(NOW)
        _chat, card = bot.drain_outbound()[0]
        assert "<b>" not in card.text
        assert "&lt;b&gt;" in card.text

    def test_ping_fires_once_with_buttons_then_never(self, world):
        repo, session, memory = world
        bot, _service = _bot(world)
        due = repo.add(
            MemoryItem(
                raw_text="Return the form",
                source="email",
                type="task",
                occurred_at=NOW + timedelta(hours=24),  # deterministic: due within the window
                child_state="proposed",
            )
        )
        repo.commit()  # the bot's ping session is a separate transaction
        pings = bot.due_pings(NOW.replace(tzinfo=None) if NOW.tzinfo is None else NOW)
        assert len(pings) == 1
        chat_id, reply = pings[0]
        assert chat_id == 7
        assert "⏰ due soon" in reply.text
        token = _token_of(reply, CB_PROPOSED_KEEP)
        assert f"{CB_PROPOSED_DROP}:{token}:0" in [d for row in reply.keyboard for _l, d in row]
        assert bot.due_pings(NOW) == []  # one ping per child, never repeated
        session.expire_all()
        session.refresh(due)
        assert due.extraction_meta.get("pinged_at") is not None


def _token_of(reply, prefix: str) -> str:
    """The registry token behind the first button with this prefix."""
    for row in reply.keyboard:
        for _label, data in row:
            if data.startswith(f"{prefix}:"):
                return data.split(":")[1]
    raise AssertionError(f"no {prefix} button in {reply.keyboard}")


class TestFixReindexes:
    """A Fix changes raw_text (the title): the child's vector must be
    re-embedded or a search for the corrected title misses it."""

    @staticmethod
    def _fake_indexer():
        class FakeIndexer:
            def __init__(self):
                self.indexed = []

            def index(self, item):
                self.indexed.append(item.raw_text)

            def remove(self, item_id):
                pass

        return FakeIndexer()

    def test_fix_reindexes_the_updated_text(self, world):
        repo, _session, _memory = world
        source = _capture(repo, SOURCE_BODY)
        child = repo.add(
            MemoryItem(raw_text="Old title", source="email", type="task",
                       child_state="proposed")
        )
        indexer = self._fake_indexer()
        apply_fix(
            repo, child, title="Corrected title", when_raw="",
            source=source, tz_name=TZ, indexer=indexer,
        )
        assert indexer.indexed == ["Corrected title"]

    def test_indexing_failure_never_fails_the_fix(self, world):
        repo, _session, _memory = world
        source = _capture(repo, SOURCE_BODY)
        child = repo.add(
            MemoryItem(raw_text="Old title", source="email", type="task",
                       child_state="proposed")
        )

        class BrokenIndexer:
            def index(self, item):
                raise RuntimeError("qdrant down")

        fixed = apply_fix(
            repo, child, title="Corrected title", when_raw="",
            source=source, tz_name=TZ, indexer=BrokenIndexer(),
        )
        assert fixed.raw_text == "Corrected title"  # the Fix landed anyway
        assert fixed.child_state == "kept"

    def test_no_indexer_still_fixes(self, world):
        repo, _session, _memory = world
        source = _capture(repo, SOURCE_BODY)
        child = repo.add(
            MemoryItem(raw_text="Old title", source="email", type="task",
                       child_state="proposed")
        )
        fixed = apply_fix(
            repo, child, title="T", when_raw="", source=source, tz_name=TZ, indexer=None
        )
        assert fixed.raw_text == "T"
