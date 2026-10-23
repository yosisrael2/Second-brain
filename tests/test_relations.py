"""PR B (spec items 20, 21, D4): item_relations + supersede tiers + dedupe key.

- item_relations (from_id, to_id, kind supersedes|corrects|derived_from|
  same_as, created_at, provenance), unique per triple.
- RelationsService: add/remove, resolve 'current version' chains.
- Supersede safety (item 20): same-thread corrections auto-APPLIED but
  visible + undoable; cross-thread LLM matches only PROPOSED (old item stays
  visible until confirmed).
- Dedupe key (item 21): same type + date ±1h + SAME applies_to + summary
  similarity >= 0.85; never merge different applies_to (R5).
- D4: superseded children struck through + restorable, hidden by default
  from /recent, /review and search.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text as sa_text

from brain.db.models import MemoryItem
from brain.db.session import SessionLocal
from brain.repositories.items import ItemsRepository


@pytest.fixture(name="ops_env")
def ops_env_fixture(clean_items: None):
    """memory_items cleaned around each test (relations cascade with them)."""
    with SessionLocal() as session:
        session.execute(sa_text("DELETE FROM item_relations"))
        session.commit()
    yield
    with SessionLocal() as session:
        session.execute(sa_text("DELETE FROM item_relations"))
        session.commit()


@pytest.fixture(name="items_repo")
def items_repo_fixture():
    with SessionLocal() as session:
        yield ItemsRepository(session)


def _svc():
    from brain.services.relations import RelationsService

    return RelationsService()


def _item(**over: Any) -> uuid.UUID:
    with SessionLocal() as session:
        item = MemoryItem(
            raw_text=over.pop("raw_text", "white shirt Thursday"),
            type=over.pop("type", None),
            occurred_at=over.pop("occurred_at", None),
            applies_to=over.pop("applies_to", None),
            child_state=over.pop("child_state", None),
        )
        session.add(item)
        session.commit()
        return item.id


def _get(item_id: uuid.UUID) -> MemoryItem:
    with SessionLocal() as session:
        item = session.get(MemoryItem, item_id)
        session.expunge(item)
        return item


class TestRelationsService:
    def test_add_and_get_relation_with_provenance(self, ops_env):
        svc = _svc()
        old, new = _item(raw_text="trip on Tuesday"), _item(raw_text="trip moved to Monday")
        rel = svc.add(new, old, kind="supersedes", provenance={"how": "same_thread"})
        assert rel.kind == "supersedes"
        assert rel.from_id == new and rel.to_id == old
        assert rel.provenance["how"] == "same_thread"

    def test_add_is_idempotent_per_triple(self, ops_env):
        svc = _svc()
        a, b = _item(raw_text="a"), _item(raw_text="b")
        first = svc.add(a, b, kind="same_as", provenance={"how": "llm_match"})
        again = svc.add(a, b, kind="same_as", provenance={"how": "llm_match"})
        assert first.id == again.id

    def test_unknown_kind_is_refused(self, ops_env):
        svc = _svc()
        a, b = _item(raw_text="a"), _item(raw_text="b")
        from brain.services.relations import RelationsError

        with pytest.raises(RelationsError):
            svc.add(a, b, kind="loves")

    def test_missing_endpoint_is_refused(self, ops_env):
        svc = _svc()
        a = _item(raw_text="a")
        from brain.services.relations import RelationsError

        with pytest.raises(RelationsError):
            svc.add(a, uuid.uuid4(), kind="same_as")

    def test_remove_relation(self, ops_env):
        svc = _svc()
        a, b = _item(raw_text="a"), _item(raw_text="b")
        rel = svc.add(a, b, kind="derived_from")
        svc.remove(rel.id)
        assert svc.relations_of(a) == []

    def test_current_version_follows_the_chain(self, ops_env):
        """v1 superseded by v2 superseded by v3 -> current is v3."""
        svc = _svc()
        v1 = _item(raw_text="trip on Tuesday")
        v2 = _item(raw_text="trip moved to Monday")
        v3 = _item(raw_text="trip on Monday 8:30")
        svc.apply_supersede(new_id=v2, old_id=v1, provenance={"how": "same_thread"})
        svc.apply_supersede(new_id=v3, old_id=v2, provenance={"how": "same_thread"})
        assert svc.current_version(v1) == v3
        assert svc.current_version(v2) == v3
        assert svc.current_version(v3) == v3

    def test_derived_from_rows_resolve_to_their_facts(self, ops_env):
        """Item 22: a derived task cites the facts it was derived from."""
        svc = _svc()
        fact1, fact2, task = (
            _item(raw_text="pick up at 12:55"),
            _item(raw_text="party on Tuesday"),
            _item(raw_text="adjust Tuesday pickup to 12:55"),
        )
        svc.add(task, fact1, kind="derived_from")
        svc.add(task, fact2, kind="derived_from")
        facts = svc.facts_of(task)
        assert sorted(facts) == sorted([fact1, fact2])


class TestSupersedeTiers:
    def test_same_thread_correction_is_applied_and_strikethrough(self, ops_env):
        svc = _svc()
        old = _item(raw_text="trip on Tuesday", child_state="kept")
        new = _item(raw_text="trip on Monday", child_state="kept")
        svc.apply_supersede(new_id=new, old_id=old, provenance={"how": "same_thread"})
        assert _get(old).superseded_by == new  # struck through, not deleted
        assert _get(new).superseded_by is None
        assert svc.current_version(old) == new

    def test_undo_supersede_restores_the_old_item(self, ops_env):
        """Item 20a: auto-applied corrections stay undoable with one tap."""
        svc = _svc()
        old, new = _item(raw_text="Tuesday trip"), _item(raw_text="Monday trip")
        rel = svc.apply_supersede(new_id=new, old_id=old, provenance={"how": "same_thread"})
        svc.undo_supersede(rel.id)
        assert _get(old).superseded_by is None
        assert svc.current_version(old) == old
        # the relation stays, marked undone (audit; never silently deleted)
        assert svc.get(rel.id).provenance.get("state") == "undone"

    def test_cross_thread_match_is_only_proposed(self, ops_env):
        """Item 20b: LLM matches across threads PROPOSE; old item stays visible."""
        svc = _svc()
        old, new = _item(raw_text="white shirt Thursday"), _item(raw_text="wear white on Thu")
        rel = svc.propose_supersede(new_id=new, old_id=old, provenance={"how": "llm_match"})
        assert rel.provenance["state"] == "proposed"
        assert _get(old).superseded_by is None  # old stays visible until confirmed
        assert svc.current_version(old) == old

    def test_confirm_promotes_proposed_to_applied(self, ops_env):
        svc = _svc()
        old, new = _item(raw_text="white shirt Thursday"), _item(raw_text="white on Thu")
        rel = svc.propose_supersede(new_id=new, old_id=old, provenance={"how": "llm_match"})
        svc.confirm_supersede(rel.id)
        assert _get(old).superseded_by == new
        assert svc.current_version(old) == new

    def test_wrong_correction_match_is_restorable(self, ops_env):
        """D4: a wrong match is reversible; the relation is never deleted."""
        svc = _svc()
        old, new = _item(raw_text="trip Tuesday"), _item(raw_text="trip Monday")
        rel = svc.apply_supersede(new_id=new, old_id=old, provenance={"how": "same_thread"})
        svc.undo_supersede(rel.id)
        assert _get(old).superseded_by is None


class TestDedupeKey:
    """Item 21: same type + date ±1h + SAME applies_to + similarity >= 0.85."""

    def svc(self):
        return _svc()

    def test_same_everything_matches(self, ops_env):
        svc = self.svc()
        base = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
        child = _item(
            raw_text="White shirt for the trip",
            type="reminder",
            occurred_at=base,
            applies_to=["Rona"],
        )
        candidate = {
            "type": "reminder",
            "occurred_at": base + timedelta(minutes=30),
            "applies_to": ["Rona"],
            "text": "White shirt for the school trip",
        }
        match = svc.find_duplicate(candidate, [child], similarity=lambda a, b: 0.9)
        assert match == child

    def test_different_applies_to_never_merges(self, ops_env):
        """R5/item 21: grade 2 vs grade 5 same day stay separate."""
        svc = self.svc()
        base = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
        rona = _item(
            raw_text="White shirt for the trip",
            type="reminder",
            occurred_at=base,
            applies_to=["Rona"],
        )
        itamar = _item(
            raw_text="White shirt for the trip",
            type="reminder",
            occurred_at=base,
            applies_to=["Itamar"],
        )
        candidate = {
            "type": "reminder",
            "occurred_at": base,
            "applies_to": ["Itamar"],
            "text": "White shirt for the trip",
        }
        match = svc.find_duplicate(candidate, [rona, itamar], similarity=lambda a, b: 1.0)
        assert match == itamar and match != rona

    def test_more_than_one_hour_apart_does_not_match(self, ops_env):
        svc = self.svc()
        base = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
        existing = _item(
            raw_text="Pickup at noon",
            type="event",
            occurred_at=base,
            applies_to=["me"],
        )
        candidate = {
            "type": "event",
            "occurred_at": base + timedelta(hours=1, minutes=1),
            "applies_to": ["me"],
            "text": "Pickup at noon",
        }
        assert svc.find_duplicate(candidate, [existing], similarity=lambda a, b: 1.0) is None

    def test_different_type_does_not_match(self, ops_env):
        svc = self.svc()
        base = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
        existing = _item(
            raw_text="White shirt", type="reminder", occurred_at=base, applies_to=["Rona"]
        )
        candidate = {
            "type": "event",
            "occurred_at": base,
            "applies_to": ["Rona"],
            "text": "White shirt",
        }
        assert svc.find_duplicate(candidate, [existing], similarity=lambda a, b: 1.0) is None

    def test_similarity_below_threshold_does_not_match(self, ops_env):
        svc = self.svc()
        base = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
        existing = _item(
            raw_text="Parent evening on Thursday",
            type="event",
            occurred_at=base,
            applies_to=["me"],
        )
        candidate = {
            "type": "event",
            "occurred_at": base,
            "applies_to": ["me"],
            "text": "Sports day on Friday",
        }
        assert svc.find_duplicate(candidate, [existing], similarity=lambda a, b: 0.5) is None

    def test_dateless_children_match_on_type_and_text_only(self, ops_env):
        """Facts have no date: the ±1h gate applies only when both have one."""
        svc = self.svc()
        existing = _item(
            raw_text="Reading book every day", type="fact", occurred_at=None, applies_to=["Rona"]
        )
        candidate = {
            "type": "fact",
            "occurred_at": None,
            "applies_to": ["Rona"],
            "text": "Reading book every day",
        }
        assert svc.find_duplicate(candidate, [existing], similarity=lambda a, b: 0.95) == existing

    def test_similarity_ratio_helper_is_deterministic(self):
        from brain.services.relations import similarity_ratio

        assert similarity_ratio("white shirt", "white shirt") > 0.99
        assert similarity_ratio("white shirt", "sports day") < 0.85


class TestSupersededHiddenByDefault:
    """D4: superseded children struck through + hidden from /recent, /review,
    search; restorable."""

    def test_recent_hides_superseded_by_default(self, ops_env, items_repo):
        svc = _svc()
        old = _item(raw_text="trip on Tuesday", child_state="kept")
        new = _item(raw_text="trip on Monday", child_state="kept")
        svc.apply_supersede(new_id=new, old_id=old, provenance={"how": "same_thread"})
        listed = items_repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
        ids = [i.id for i in listed]
        assert new in ids and old not in ids
        incl = items_repo.list(
            item_type=None,
            project=None,
            status=None,
            limit=50,
            offset=0,
            include_superseded=True,
        )
        assert old in [i.id for i in incl]

    def test_search_hides_superseded_by_default(self, ops_env, items_repo):
        svc = _svc()
        old = _item(raw_text="zebra crossing permission slip", child_state="kept")
        new = _item(raw_text="zebra crossing permission slip updated", child_state="kept")
        svc.apply_supersede(new_id=new, old_id=old, provenance={"how": "same_thread"})
        hits = items_repo.search(query="zebra crossing", limit=50, offset=0)
        assert new in [i.id for i in hits]
        assert old not in [i.id for i in hits]
        hits_all = items_repo.search(
            query="zebra crossing", limit=50, offset=0, include_superseded=True
        )
        assert old in [i.id for i in hits_all]

    def test_restore_brings_the_item_back(self, ops_env, items_repo):
        svc = _svc()
        old, new = _item(raw_text="trip on Tuesday"), _item(raw_text="trip on Monday")
        rel = svc.apply_supersede(new_id=new, old_id=old, provenance={"how": "same_thread"})
        svc.undo_supersede(rel.id)
        listed = items_repo.list(item_type=None, project=None, status=None, limit=50, offset=0)
        assert old in [i.id for i in listed]


class TestRelationsApi:
    def test_add_list_current_round_trip(self, client, ops_env):
        old = _item(raw_text="trip on Tuesday")
        new = _item(raw_text="trip on Monday")
        response = client.post(
            f"/items/{new}/relations",
            json={"to_id": str(old), "kind": "supersedes", "provenance": {"how": "same_thread"}},
        )
        assert response.status_code == 201
        listing = client.get(f"/items/{new}/relations").json()
        assert any(r["kind"] == "supersedes" and r["to_id"] == str(old) for r in listing)
        current = client.get(f"/items/{old}/current")
        assert current.status_code == 200
        assert current.json()["id"] == str(new)

    def test_propose_confirm_undo_endpoints(self, client, ops_env):
        old, new = _item(raw_text="white shirt Thursday"), _item(raw_text="white on Thu")
        rel = client.post(
            f"/items/{new}/relations",
            json={
                "to_id": str(old),
                "kind": "supersedes",
                "provenance": {"how": "llm_match", "state": "proposed"},
                "propose": True,
            },
        ).json()
        assert rel["provenance"]["state"] == "proposed"
        confirmed = client.post(f"/items/relations/{rel['id']}/confirm").json()
        assert confirmed["provenance"]["state"] == "applied"
        undone = client.post(f"/items/relations/{rel['id']}/undo").json()
        assert undone["provenance"]["state"] == "undone"
        assert _get(old).superseded_by is None

    def test_unknown_kind_is_422(self, client, ops_env):
        a, b = _item(raw_text="a"), _item(raw_text="b")
        response = client.post(f"/items/{a}/relations", json={"to_id": str(b), "kind": "loves"})
        assert response.status_code == 422
