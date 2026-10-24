"""PR D-recon-2a: the verdict cards (plan /private/tmp/second-brain-w9-plans/recon2-plan.md).

Reuses #73's persisted shape (children carry extraction_meta.recon_verdict /
recon_fields / recon_matched_id; ItemRelation kinds supersedes / conflicts /
cancels / details with provenance.how='recon') and PR B's bot machinery
(pending registry, <64B callbacks, HTML escaping, state-guarded transitions):

- UPDATE (proposed): the child's row is ✅ Confirm update / ✖ Keep both —
  never Keep/Fix/Drop; a same-thread auto-apply shows ↩︎ applied + Undo only.
- CONFLICT: both versions side by side + "which is right?" — keep existing
  (discard + drop the child) / keep new (apply the supersede).
- CANCELLATION: ✅ Confirm cancellation (strikes the target) / ✖ Not
  cancelled; the target's card later carries ♻️ Restore.
- MORE_DETAIL: a 🔔 line on the existing item's card, no buttons.
- ALREADY_KNOWN: creates no child, so no card is ever sent.
- Nothing ever applies without a tap; a stale callback after the relation
  changed answers "already handled".

Offline: DB-backed tests run the REAL TelegramBot over the *_test database
(same pattern as tests/test_relations_bot.py); no Telegram messages, no
live writes.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from brain.db.models import ItemRelation, ItemSource, MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository, default_hidden_conditions
from brain.services.relations_view import (
    CB_RECON_DISCARD,
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
        session.execute(text("DELETE FROM item_sources"))
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


_KINDS = {"update": "supersedes", "conflict": "conflicts",
          "cancellation": "cancels", "more_detail": "details"}


def _recon_child(
    repo: ItemsRepository,
    session: Session,
    *,
    source: MemoryItem,
    matched: MemoryItem,
    verdict: str,
    state: str = "proposed",
    fields: dict | None = None,
    text: str = "the new version",
) -> MemoryItem:
    """A recon verdict child + its relation, exactly as #73's write() parks them."""
    fields = fields if fields is not None else {"when": ["Friday 10:00", "Saturday 10:00"]}
    child = repo.add(MemoryItem(
        raw_text=text,
        child_state="proposed",
        extraction_meta={
            "recon_verdict": verdict,
            "recon_fields": fields,
            "recon_matched_id": str(matched.id),
        },
    ))
    session.add(ItemSource(child_id=child.id, source_id=source.id, quote=text))
    provenance: dict = {"how": "recon"}
    if verdict == "update":
        provenance |= {"state": state, "verdict": "update", "fields": fields}
    elif verdict == "conflict":
        provenance["fields"] = fields
    elif verdict == "cancellation":
        provenance["state"] = "proposed"
    session.add(ItemRelation(
        from_id=child.id, to_id=matched.id, kind=_KINDS[verdict], provenance=provenance,
    ))
    if verdict == "update" and state == "applied":  # #73's auto-apply strikes the target
        matched.superseded_by = child.id
    repo.commit()
    return child


def _hidden(session: Session, item_id: uuid.UUID) -> bool:
    """Would default_hidden_conditions() hide this row from every SQL leg?"""
    stmt = (
        session.query(MemoryItem.id)
        .filter(MemoryItem.id == item_id)
        .filter(default_hidden_conditions()[0])
        .filter(default_hidden_conditions()[1])
    )
    return stmt.first() is None


class TestUpdateCards:
    def test_proposed_update_row_replaces_keep_fix_drop(self, world):
        repo, session = world
        source = _capture(repo, "email: family dinner plan")
        old = _capture(repo, "family dinner on Friday")
        _recon_child(repo, session, source=source, matched=old, verdict="update",
                     text="family dinner moved to Saturday")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)

        assert reply is not None
        assert "↩︎ proposes:" in reply.text
        assert "when: Friday 10:00 → Saturday 10:00" in reply.text
        assert "✅ Confirm update" in _labels(reply)
        assert "✖ Keep both" in _labels(reply)
        # the recon verdict owns the row: no Keep/Fix/Drop for it
        assert "✏️ Fix" not in _labels(reply)
        confirm = _find(reply, CB_REL_CONFIRM)
        assert len(confirm) < 64

    def test_mixed_card_keeps_default_rows_for_non_recon_children(self, world):
        repo, session = world
        source = _capture(repo, "email: plans")
        old = _capture(repo, "family dinner on Friday")
        _recon_child(repo, session, source=source, matched=old, verdict="update",
                     text="family dinner moved to Saturday")
        plain = repo.add(MemoryItem(raw_text="buy wine", child_state="proposed"))
        session.add(ItemSource(child_id=plain.id, source_id=source.id, quote="buy wine"))
        repo.commit()
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)

        labels = _labels(reply)
        assert "✅ Confirm update" in labels
        assert "✏️ Fix" in labels  # the plain child keeps its row

    def test_confirm_update_applies_and_strikes_the_old_item(self, world):
        repo, session = world
        source = _capture(repo, "email: family dinner plan")
        old = _capture(repo, "family dinner on Friday")
        child = _recon_child(repo, session, source=source, matched=old, verdict="update",
                             text="family dinner moved to Saturday")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        data = _find(reply, CB_REL_CONFIRM)

        tapped = bot.handle_callback(data, 7)
        session.expire_all()
        assert session.get(MemoryItem, old.id).superseded_by == child.id
        rel = session.query(ItemRelation).filter_by(from_id=child.id).one()
        assert (rel.provenance or {}).get("state") == "applied"
        assert session.get(MemoryItem, child.id).child_state == "kept"
        assert _hidden(session, old.id)
        # the confirmed child is kept, so it leaves the proposal card:
        # the honest close is the notice + all-done, not an applied row
        # (the ↩︎ Undo row belongs to the AUTO-apply card, see above)
        assert "✅ Confirmed" in tapped.text
        assert "✓ all done here." in tapped.text

    def test_keep_both_discards_and_keeps_both_visible(self, world):
        repo, session = world
        source = _capture(repo, "email: family dinner plan")
        old = _capture(repo, "family dinner on Friday")
        child = _recon_child(repo, session, source=source, matched=old, verdict="update",
                             text="family dinner moved to Saturday")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        data = _find(reply, CB_REL_KEEP_BOTH)

        tapped = bot.handle_callback(data, 7)
        session.expire_all()
        assert session.query(ItemRelation).filter_by(from_id=child.id).count() == 0
        assert session.get(MemoryItem, old.id).superseded_by is None
        assert session.get(MemoryItem, child.id).child_state == "kept"
        assert _hidden(session, old.id) is False
        assert "✖" in tapped.text

    def test_auto_applied_update_card_shows_undo_only(self, world):
        repo, session = world
        source = _capture(repo, "email: family dinner plan")
        old = _capture(repo, "family dinner on Friday")
        _recon_child(repo, session, source=source, matched=old, verdict="update",
                     state="applied", text="family dinner moved to Saturday")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)

        assert "↩︎ applied:" in reply.text
        labels = _labels(reply)
        assert "↩︎ Undo" in labels
        assert "✅ Confirm update" not in labels
        assert "✖ Keep both" not in labels

    def test_undo_after_auto_apply_unstrikes(self, world):
        repo, session = world
        source = _capture(repo, "email: family dinner plan")
        old = _capture(repo, "family dinner on Friday")
        child = _recon_child(repo, session, source=source, matched=old, verdict="update",
                             state="applied", text="family dinner moved to Saturday")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        data = _find(reply, CB_REL_UNDO)

        tapped = bot.handle_callback(data, 7)
        session.expire_all()
        assert session.get(MemoryItem, old.id).superseded_by is None
        rel = session.query(ItemRelation).filter_by(from_id=child.id).one()
        assert (rel.provenance or {}).get("state") == "undone"
        assert "↩️" in tapped.text


class TestConflictCards:
    def test_conflict_card_shows_both_versions_and_question(self, world):
        repo, session = world
        source = _capture(repo, "whatsapp: cousin's birthday")
        old = _capture(repo, "birthday dinner at Nana's")
        _recon_child(repo, session, source=source, matched=old, verdict="conflict",
                     text="birthday dinner at Daniel's", )
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)

        assert "⚠️ conflicts" in reply.text
        assert "which is right?" in reply.text
        assert "birthday dinner at Nana's" in reply.text  # the existing version
        assert "birthday dinner at Daniel's" in reply.text  # the new version
        labels = _labels(reply)
        assert "✅ keep existing" in labels
        assert "✅ keep new" in labels
        assert "✏️ Fix" not in labels
        keep_existing = _find(reply, CB_RECON_DISCARD)
        keep_new = _find(reply, CB_REL_CONFIRM)
        assert len(keep_existing) < 64 and len(keep_new) < 64

    def test_conflict_keep_new_applies_the_supersede(self, world):
        repo, session = world
        source = _capture(repo, "whatsapp: cousin's birthday")
        old = _capture(repo, "birthday dinner at Nana's")
        child = _recon_child(repo, session, source=source, matched=old, verdict="conflict",
                             text="birthday dinner at Daniel's")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        data = _find(reply, CB_REL_CONFIRM)

        tapped = bot.handle_callback(data, 7)
        session.expire_all()
        # keep new = the new child replaces the old item (struck, relation applied)
        assert session.get(MemoryItem, old.id).superseded_by == child.id
        rel = session.query(ItemRelation).filter_by(from_id=child.id).one()
        assert rel.kind == "conflicts"
        assert (rel.provenance or {}).get("state") == "applied"
        assert session.get(MemoryItem, child.id).child_state == "kept"
        assert "✅" in tapped.text

    def test_conflict_keep_existing_discards_and_drops_the_child(self, world, monkeypatch):
        from brain import ops

        seen: list[tuple] = []
        monkeypatch.setattr(ops, "record_event",
                            lambda *a, **k: seen.append((a, k)))
        repo, session = world
        source = _capture(repo, "whatsapp: cousin's birthday")
        old = _capture(repo, "birthday dinner at Nana's")
        child = _recon_child(repo, session, source=source, matched=old, verdict="conflict",
                             text="birthday dinner at Daniel's")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        data = _find(reply, CB_RECON_DISCARD)

        tapped = bot.handle_callback(data, 7)
        session.expire_all()
        # keep existing = the proposal goes away, the child is dropped, old untouched
        assert session.query(ItemRelation).filter_by(from_id=child.id).count() == 0
        assert session.get(MemoryItem, child.id).child_state == "dropped"
        assert session.get(MemoryItem, old.id).superseded_by is None
        assert _hidden(session, old.id) is False
        assert "✅" in tapped.text
        assert any("recon_conflict_resolved" in str(a) for a, _k in seen)

    def test_conflict_keep_new_restore_on_the_target_card(self, world):
        """keep-new strikes the old item; its card carries ♻️ Restore (the
        standard undo path on the applied relation)."""
        repo, session = world
        source = _capture(repo, "whatsapp: cousin's birthday")
        old = _capture(repo, "birthday dinner at Nana's")
        child = _recon_child(repo, session, source=source, matched=old, verdict="conflict",
                             text="birthday dinner at Daniel's")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        bot.handle_callback(_find(reply, CB_REL_CONFIRM), 7)
        session.expire_all()

        token = bot.registry.put(_PendingIds(ids=(old.id,)))
        opened = bot.handle_callback(f"o:{token}:0", 7)
        restore = _find(opened, CB_REL_RESTORE)

        tapped = bot.handle_callback(restore, 7)
        session.expire_all()
        assert session.get(MemoryItem, old.id).superseded_by is None
        rel = session.query(ItemRelation).filter_by(from_id=child.id).one()
        assert (rel.provenance or {}).get("state") == "undone"
        assert _hidden(session, old.id) is False
        assert "♻️" in tapped.text

    def test_conflict_double_tap_answers_already_handled(self, world):
        repo, session = world
        source = _capture(repo, "whatsapp: cousin's birthday")
        old = _capture(repo, "birthday dinner at Nana's")
        child = _recon_child(repo, session, source=source, matched=old, verdict="conflict",
                             text="birthday dinner at Daniel's")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        data = _find(reply, CB_REL_CONFIRM)

        bot.handle_callback(data, 7)
        again = bot.handle_callback(data, 7)
        assert "already handled" in again.text
        session.expire_all()
        assert session.get(MemoryItem, old.id).superseded_by == child.id  # not re-struck twice


class TestCancellationCards:
    def test_cancellation_confirm_strikes_the_target(self, world):
        repo, session = world
        source = _capture(repo, "whatsapp: plan change")
        target = _capture(repo, "gym session on Thursday")
        child = _recon_child(repo, session, source=source, matched=target,
                             verdict="cancellation", text="gym session on Thursday is off")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        data = _find(reply, CB_REL_CONFIRM)

        assert "🗑 cancels:" in reply.text
        bot.handle_callback(data, 7)
        session.expire_all()
        target_row = session.get(MemoryItem, target.id)
        assert (target_row.extraction_meta or {}).get("cancelled") is not None
        rel = session.query(ItemRelation).filter_by(from_id=child.id).one()
        assert (rel.provenance or {}).get("state") == "applied"
        assert session.get(MemoryItem, child.id).child_state == "kept"
        assert _hidden(session, target.id)

    def test_cancellation_restore_on_the_target_card(self, world):
        repo, session = world
        source = _capture(repo, "whatsapp: plan change")
        target = _capture(repo, "gym session on Thursday")
        child = _recon_child(repo, session, source=source, matched=target,
                             verdict="cancellation", text="gym session on Thursday is off")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        bot.handle_callback(_find(reply, CB_REL_CONFIRM), 7)
        session.expire_all()

        # the struck target's card carries ♻️ Restore (the plan's later ♻️)
        token = bot.registry.put(_PendingIds(ids=(target.id,)))
        opened = bot.handle_callback(f"o:{token}:0", 7)
        restore = _find(opened, CB_REL_RESTORE)
        assert len(restore) < 64

        tapped = bot.handle_callback(restore, 7)
        session.expire_all()
        target_row = session.get(MemoryItem, target.id)
        assert (target_row.extraction_meta or {}).get("cancelled") is None
        rel = session.query(ItemRelation).filter_by(from_id=child.id).one()
        assert (rel.provenance or {}).get("state") == "undone"
        assert _hidden(session, target.id) is False
        assert "♻️" in tapped.text

    def test_cancellation_not_cancelled_discards(self, world):
        repo, session = world
        source = _capture(repo, "whatsapp: plan change")
        target = _capture(repo, "gym session on Thursday")
        child = _recon_child(repo, session, source=source, matched=target,
                             verdict="cancellation", text="gym session on Thursday is off")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        data = _find(reply, CB_RECON_DISCARD)

        tapped = bot.handle_callback(data, 7)
        session.expire_all()
        assert session.query(ItemRelation).filter_by(from_id=child.id).count() == 0
        assert session.get(MemoryItem, child.id).child_state == "dropped"
        assert (session.get(MemoryItem, target.id).extraction_meta or {}).get("cancelled") is None
        assert _hidden(session, target.id) is False
        assert "✖" in tapped.text


class TestMoreDetailCards:
    def test_more_detail_line_on_the_matched_item_card_without_buttons(self, world):
        repo, session = world
        source = _capture(repo, "email: birthday details")
        matched = _capture(repo, "Noa's birthday")
        _recon_child(repo, session, source=source, matched=matched, verdict="more_detail",
                     text="Noa's birthday at 19:00, bring the dog", fields={})
        bot, _ = _bot(world)

        token = bot.registry.put(_PendingIds(ids=(matched.id,)))
        opened = bot.handle_callback(f"o:{token}:0", 7)
        assert "🔔 more detail captured:" in opened.text
        assert "Noa's birthday at 19:00, bring the dog" in opened.text
        assert opened.keyboard is None  # the 🔔 line adds no buttons


class TestAlreadyKnown:
    def test_already_known_sends_no_card(self, world):
        from brain.services.extraction.service import SourceOutcome

        repo, _ = world
        source = _capture(repo, "email: someone mentions Noa's birthday again")
        bot, _ = _bot(world)

        bot.proposals_changed([SourceOutcome(
            source=source,
            created=0,
            updated=0,
        )])
        assert bot.drain_outbound() == []  # silence — ALREADY_KNOWN never cards


class TestStaleGuard:
    def test_stale_confirm_after_relation_changed_answers_already_handled(self, world):
        repo, session = world
        source = _capture(repo, "email: family dinner plan")
        old = _capture(repo, "family dinner on Friday")
        _recon_child(repo, session, source=source, matched=old, verdict="update",
                     text="family dinner moved to Saturday")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        data = _find(reply, CB_REL_CONFIRM)

        bot.handle_callback(data, 7)
        again = bot.handle_callback(data, 7)
        assert "already handled" in again.text

    def test_keep_both_on_an_applied_relation_answers_already_handled(self, world):
        """The state guard must also protect the destructive keep-both path:
        a stale ✖ after a confirm must never delete an applied relation."""
        repo, session = world
        source = _capture(repo, "email: family dinner plan")
        old = _capture(repo, "family dinner on Friday")
        _recon_child(repo, session, source=source, matched=old, verdict="update",
                     text="family dinner moved to Saturday")
        bot, _ = _bot(world)
        reply = bot._proposal_reply_for(source)
        confirm = _find(reply, CB_REL_CONFIRM)
        token = confirm.split(":")[1]

        bot.handle_callback(confirm, 7)
        stale = bot.handle_callback(f"{CB_REL_KEEP_BOTH}:{token}:0", 7)
        assert "already handled" in stale.text
        session.expire_all()
        rel = session.query(ItemRelation).one()
        assert (rel.provenance or {}).get("state") == "applied"  # still applied


class _PendingIds:
    """Minimal Pending stand-in: the bot only reads .ids/.kind/.text/.step."""

    def __init__(self, ids) -> None:
        self.ids = tuple(ids)
        self.kind = "capture"
        self.text = None
        self.step = None
