"""PR B bot surfaces: Undo / Confirm / Keep-both / Restore on relations.

Item 20a — an applied same-thread correction's card shows
"↩︎ replaced: <old> → <new>" with an ↩︎ Undo button.
Item 20b — a proposed cross-thread supersede shows ✅ Confirm replace /
✖ Keep both.
D4 — a superseded item renders struck-through with ♻️ Restore when opened.

Offline: DB-backed tests run the REAL TelegramBot over the *_test database
(same pattern as the proposal-card tests); no Telegram messages, no live
writes. Callback data stays < 64 bytes because relation ids ride the bot's
pending registry (worker_6's pattern).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from brain.db.models import MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.services.relations import RelationsService
from brain.services.relations_view import (
    CB_REL_CONFIRM,
    CB_REL_KEEP_BOTH,
    CB_REL_RESTORE,
    CB_REL_UNDO,
)
from conftest import assert_test_database

TZ = "Asia/Jerusalem"


class _Wipe:
    @staticmethod
    def wipe(session: Session) -> None:
        session.execute(text("DELETE FROM item_relations"))
        session.execute(text("DELETE FROM memory_items"))
        session.commit()


@pytest.fixture(name="world")
def world_fixture(database: None):
    assert_test_database(str(engine.url))
    session = SessionLocal()
    _Wipe.wipe(session)
    repo = ItemsRepository(session)
    yield repo, session
    session.rollback()
    session.close()
    _Wipe.wipe(session)


def _capture(repo: ItemsRepository, body: str, **over) -> MemoryItem:
    item = repo.add(MemoryItem(raw_text=body, **over))
    repo.commit()  # the bot's relation sessions are separate transactions
    return item


def _bot(world):
    from brain.telegram_bot import BotServices, TelegramBot

    repo, session = world
    from brain.services.items import ItemsService

    items = ItemsService(repo)

    @contextmanager
    def factory():
        yield BotServices(ingest=None, items=items, search=None, ask=None)

    return (
        TelegramBot(
            services_factory=factory,
            allowed_ids=frozenset({7}),
            timezone=ZoneInfo(TZ),
        ),
        session,
    )


def _labels(reply) -> list[str]:
    return [label for row in reply.keyboard or [] for label, _ in row]


def _find(reply, prefix: str) -> str:
    for row in reply.keyboard or []:
        for _label, data in row:
            if data.startswith(f"{prefix}:"):
                return data
    raise AssertionError(f"no {prefix} button in {_labels(reply)}")


def _token_of(reply, prefix: str) -> str:
    return _find(reply, prefix).split(":")[1]


class TestOpenedSupersededItem:
    def test_open_card_renders_struck_through_with_restore(self, world):
        repo, session = world
        old = _capture(repo, "trip on Tuesday")
        new = _capture(repo, "trip moved to Monday")
        RelationsService().apply_supersede(new_id=new.id, old_id=old.id)
        session.expire_all()  # the fixture session outlives the write (fresh per action in prod)

        bot, _ = _bot(world)
        token = bot.registry.put(_PendingIds(ids=(old.id,)))
        reply = bot.handle_callback(f"o:{token}:0", 7)

        assert "<s>" in reply.text and "</s>" in reply.text  # struck-through
        assert "↪︎ replaced by:" in reply.text
        assert "trip moved to Monday" in reply.text
        restore = _find(reply, CB_REL_RESTORE)
        assert len(restore) < 64

    def test_restore_callback_unstrikes_the_item(self, world):
        repo, session = world
        old = _capture(repo, "trip on Tuesday")
        new = _capture(repo, "trip moved to Monday")
        RelationsService().apply_supersede(new_id=new.id, old_id=old.id)
        session.expire_all()

        bot, _ = _bot(world)
        token = bot.registry.put(_PendingIds(ids=(old.id,)))
        reply = bot.handle_callback(f"{CB_REL_RESTORE}:{token}", 7)

        session.expire_all()
        assert session.get(MemoryItem, old.id).superseded_by is None
        assert "♻️ Restored" in reply.text


class TestAppliedCorrectionCard:
    def test_card_shows_replaced_notice_and_undo_button(self, world):
        repo, _ = world
        old = _capture(repo, "trip on Tuesday")
        new = _capture(repo, "trip moved to Monday")
        RelationsService().apply_supersede(new_id=new.id, old_id=old.id)

        bot, _ = _bot(world)
        token = bot.registry.put(_PendingIds(ids=(new.id, old.id)))
        reply = bot._card_reply(bot.registry.get(token))

        assert "↩︎ replaced:" in reply.text
        assert "trip on Tuesday" in reply.text
        assert "trip moved to Monday" in reply.text
        undo = _find(reply, CB_REL_UNDO)
        assert len(undo) < 64

    def test_undo_callback_marks_the_relation_undone(self, world):
        repo, session = world
        old = _capture(repo, "trip on Tuesday")
        new = _capture(repo, "trip moved to Monday")
        rel = RelationsService().apply_supersede(new_id=new.id, old_id=old.id)
        session.expire_all()

        bot, _ = _bot(world)
        token = bot.registry.put(_PendingIds(ids=(new.id, rel.id)))
        reply = bot.handle_callback(f"{CB_REL_UNDO}:{token}:0", 7)

        session.expire_all()
        assert session.get(MemoryItem, old.id).superseded_by is None
        assert RelationsService().get(rel.id).provenance["state"] == "undone"
        assert "↩️ Undone" in reply.text


class TestProposedCrossThreadSupersede:
    def test_card_shows_confirm_and_keep_both_buttons(self, world):
        repo, _ = world
        old = _capture(repo, "pool party at Dana's")
        new = _capture(repo, "pool party moved to Friday")
        RelationsService().propose_supersede(new_id=new.id, old_id=old.id)

        bot, _ = _bot(world)
        token = bot.registry.put(_PendingIds(ids=(new.id,)))
        reply = bot._card_reply(bot.registry.get(token))

        assert "❓" in reply.text and "maybe replaces" in reply.text
        labels = _labels(reply)
        assert "✅ Confirm replace" in labels
        assert "✖ Keep both" in labels
        assert len(_find(reply, CB_REL_CONFIRM)) < 64
        assert len(_find(reply, CB_REL_KEEP_BOTH)) < 64

    def test_confirm_callback_applies_and_strikes(self, world):
        repo, session = world
        old = _capture(repo, "pool party at Dana's")
        new = _capture(repo, "pool party moved to Friday")
        rel = RelationsService().propose_supersede(new_id=new.id, old_id=old.id)
        session.expire_all()

        bot, _ = _bot(world)
        token = bot.registry.put(_PendingIds(ids=(new.id, rel.id)))
        bot.handle_callback(f"{CB_REL_CONFIRM}:{token}:0", 7)

        session.expire_all()
        assert session.get(MemoryItem, old.id).superseded_by == new.id
        assert RelationsService().get(rel.id).provenance["state"] == "applied"

    def test_keep_both_callback_discards_the_proposal(self, world):
        repo, session = world
        old = _capture(repo, "pool party at Dana's")
        new = _capture(repo, "pool party moved to Friday")
        rel = RelationsService().propose_supersede(new_id=new.id, old_id=old.id)
        session.expire_all()

        bot, _ = _bot(world)
        token = bot.registry.put(_PendingIds(ids=(new.id, rel.id)))
        reply = bot.handle_callback(f"{CB_REL_KEEP_BOTH}:{token}:0", 7)

        session.expire_all()
        assert RelationsService().get(rel.id) is None
        assert session.get(MemoryItem, old.id).superseded_by is None
        assert "Keeping both" in reply.text


class TestCallbackHygiene:
    def test_all_relation_callbacks_stay_under_64_bytes(self, world):
        repo, _ = world
        old = _capture(repo, "old")
        new = _capture(repo, "new")
        rel = RelationsService().apply_supersede(new_id=new.id, old_id=old.id)
        world[1].expire_all()
        bot, _ = _bot(world)
        token = bot.registry.put(_PendingIds(ids=(new.id, rel.id, old.id)))
        for prefix in (CB_REL_UNDO, CB_REL_CONFIRM, CB_REL_KEEP_BOTH, CB_REL_RESTORE):
            sep = ":0" if prefix != CB_REL_RESTORE else ""
            assert len(f"{prefix}:{token}{sep}") < 64

    def test_stale_token_gets_the_graceful_reply(self, world):
        bot, _ = _bot(world)
        reply = bot.handle_callback(f"{CB_REL_RESTORE}:{uuid.uuid4().hex[:8]}", 7)
        assert "expired" in reply.text or "Unknown" in reply.text

    def test_user_text_is_escaped_in_notices(self, world):
        repo, _ = world
        old = _capture(repo, "task <b>important</b> & urgent")
        new = _capture(repo, "task rescheduled")
        RelationsService().apply_supersede(new_id=new.id, old_id=old.id)
        bot, _ = _bot(world)
        token = bot.registry.put(_PendingIds(ids=(new.id,)))
        reply = bot._card_reply(bot.registry.get(token))
        assert "<b>important</b>" not in reply.text
        assert "&lt;b&gt;important" in reply.text


class _PendingIds:
    """Minimal Pending stand-in: the bot only reads .ids/.kind/.text/.step."""

    def __init__(self, ids) -> None:
        self.ids = tuple(ids)
        self.kind = "relation"
        self.text = ""
        self.step = 0
