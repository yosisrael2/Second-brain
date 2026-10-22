"""Memory layer tests: versioned facts, applies_to resolution, prompt block."""

import uuid

import pytest
from sqlalchemy import text

from brain.db.session import SessionLocal
from brain.repositories.memory import MemoryRepository
from brain.services.memory import MemoryError, MemoryService

RONA = {
    "gender": "girl", "school": "בראשית", "grade": "5",
    "class": "ה' ספיר", "teacher": "Rinat",
}
ITAMAR = {
    "gender": "boy", "school": "בראשית", "grade": "2",
    "class": "ב' ברקת", "teacher": "Maayan",
}


@pytest.fixture(name="memory")
def memory_fixture() -> MemoryService:
    """A clean memory world per test; the session closes with the test.

    Owning the session here (and closing it) is what keeps the pool from
    exhausting across ~20 tests.
    """
    session = SessionLocal()
    try:
        session.execute(text("DELETE FROM memory_facts"))
        session.execute(text("DELETE FROM entity_aliases"))
        session.execute(text("DELETE FROM entities"))
        session.commit()
        yield MemoryService(MemoryRepository(session))
    finally:
        session.rollback()
        session.close()


def _seed_world(service: MemoryService) -> None:
    service.set_kid("Rona", **{k.replace("class", "class_"): v for k, v in RONA.items()})
    service.set_kid("Itamar", **{k.replace("class", "class_"): v for k, v in ITAMAR.items()})


def _find_fact(service: MemoryService, predicate: str, value: str):
    for fact in service._repo.current_facts(status="confirmed"):
        if fact.predicate == predicate and fact.value == value:
            return fact
    return None


# -- resolve_applies_to (design item 28 acceptance matrix) ----------------------


def test_grade_5_6_parents_map_to_me(memory: MemoryService):
    _seed_world(memory)
    assert memory.resolve_applies_to(
        {"school": "בראשית", "grades": "ה-ו", "role": "parents"}
    ) == ["me"]


def test_girls_of_class_excludes_a_boy(memory: MemoryService):
    _seed_world(memory)
    # The girls' dialogue: girls of ב' ברקת — Itamar is a boy (explicit fact).
    assert memory.resolve_applies_to(
        {"school": "בראשית", "class": "ב' ברקת", "gender": "girls"}
    ) == ["nobody"]


def test_whole_school_audience_returns_all_kids(memory: MemoryService):
    _seed_world(memory)
    assert memory.resolve_applies_to({"school": "בראשית"}) == ["Rona", "Itamar"]


def test_class_audience_returns_that_class_kid(memory: MemoryService):
    _seed_world(memory)
    assert (
        memory.resolve_applies_to({"school": "בראשית", "class": "ה' ספיר"}) == ["Rona"]
    )


def test_no_audience_is_unknown_never_nobody(memory: MemoryService):
    _seed_world(memory)
    assert memory.resolve_applies_to(None) == ["unknown"]
    assert memory.resolve_applies_to({}) == ["unknown"]


def test_empty_memory_is_unknown_never_nobody(memory: MemoryService):
    assert memory.resolve_applies_to({"school": "בראשית"}) == ["unknown"]


def test_numeric_grade_range_also_matches(memory: MemoryService):
    _seed_world(memory)
    assert (
        memory.resolve_applies_to({"school": "בראשית", "grades": "5-6"}) == ["Rona"]
    )


def test_gendered_school_wide_audience_excludes_the_other_gender(memory: MemoryService):
    _seed_world(memory)
    assert (
        memory.resolve_applies_to({"school": "בראשית", "gender": "boys"})
        == ["Itamar"]
    )


def test_missing_gender_fact_is_unknown_not_nobody(memory: MemoryService):
    """A gendered audience + no stored gender = unconfirmable -> unknown."""
    memory.set_kid("Niv", school="בראשית", grade="3", class_="ג' כהן")
    assert (
        memory.resolve_applies_to({"school": "בראשית", "class": "ג' כהן", "gender": "girls"})
        == ["unknown"]
    )


def test_named_kids_need_known_memory(memory: MemoryService):
    _seed_world(memory)
    assert memory.resolve_applies_to({"named_kids": ["Rona"]}) == ["Rona"]
    assert memory.resolve_applies_to({"named_kids": ["Yarden"]}) == ["unknown"]


def test_staff_role_applies_to_nobody(memory: MemoryService):
    _seed_world(memory)
    assert memory.resolve_applies_to({"school": "בראשית", "role": "staff"}) == ["nobody"]


def test_unparseable_grade_range_is_unknown_not_nobody(memory: MemoryService):
    _seed_world(memory)
    assert (
        memory.resolve_applies_to({"school": "בראשית", "grades": "???"})
        == ["unknown"]
    )


# -- fact versioning (close + open, never in place) ------------------------------


def test_set_kid_closes_old_version_and_opens_a_new_one(memory: MemoryService):
    _seed_world(memory)
    old = _find_fact(memory, "grade", "5")
    assert old is not None

    memory.set_kid("Rona", grade="6")

    assert _find_fact(memory, "grade", "5") is None  # no longer current
    history = memory.fact_history(old.id)
    assert [row["value"] for row in history] == ["5", "6"]
    assert history[0]["valid_to"] is not None  # the old version is closed
    assert history[1]["supersedes_id"] == history[0]["id"]
    kids = memory.kids()
    assert [kid["grade"] for kid in kids if kid["name"] == "Rona"] == ["6"]


def test_set_kid_is_idempotent_for_unchanged_values(memory: MemoryService):
    _seed_world(memory)
    old = _find_fact(memory, "grade", "5")
    memory.set_kid("Rona", grade="5")  # same value: no new version
    assert memory.fact_history(old.id) != []  # anchor still exists
    grades = [
        f for f in memory._repo.facts_about(old.subject_id) if f.predicate == "grade"
    ]
    assert len(grades) == 1


def test_patch_fact_creates_a_version(memory: MemoryService):
    _seed_world(memory)
    old = _find_fact(memory, "teacher", "Rinat")
    new_version = memory.patch_fact(old.id, value="Rinat S.")
    assert new_version["supersedes_id"] == str(old.id)
    assert new_version["status"] == "confirmed"  # status inherited
    assert [row["value"] for row in memory.fact_history(old.id)] == ["Rinat", "Rinat S."]


# -- status lifecycle --------------------------------------------------------------


def test_proposed_facts_drive_nothing_until_confirmed(memory: MemoryService):
    _seed_world(memory)
    me = memory.add_entity("person", "me")
    proposal = memory.add_fact(
        memory._ensure_entity(kind="person", name="me").id,
        "note",
        value="prefers Hebrew replies",
        status="proposed",
    )
    assert "prefers Hebrew replies" not in memory.compact_block()
    confirmed = memory.confirm_fact(uuid.UUID(proposal["id"]))
    assert confirmed["status"] == "confirmed"
    assert "prefers Hebrew replies" in memory.compact_block()
    # me entity remains unused-but-present for the API test below
    assert me["kind"] == "person"


def test_confirm_non_proposed_raises(memory: MemoryService):
    fact = memory.remember("a confirmed note")
    with pytest.raises(MemoryError):
        memory.confirm_fact(uuid.UUID(fact["id"]))


def test_soft_delete_keeps_history(memory: MemoryService):
    fact = memory.remember("buy stamps")
    removed = memory.delete_fact(uuid.UUID(fact["id"]))
    assert removed["status"] == "rejected"
    assert removed["valid_to"] is not None
    with pytest.raises(MemoryError):
        memory.delete_fact(uuid.UUID(fact["id"]))  # already closed -> conflict
    assert "buy stamps" not in memory.compact_block()


# -- remember / compact block -------------------------------------------------------


def test_remember_targets_a_me_entity(memory: MemoryService):
    fact = memory.remember("family calendar is organized by Zohar")
    overview = memory.overview(q="me")
    assert overview, "a 'me' person entity must exist after remember()"
    assert fact["subject_id"] == overview[0]["id"]
    assert fact["predicate"] == "note"
    assert fact["status"] == "confirmed"


def test_compact_block_empty_memory_renders_empty(memory: MemoryService):
    """Empty memory -> '' -> the ask prompt stays byte-identical (pinned)."""
    assert memory.compact_block() == ""


def test_compact_block_lists_kids_and_notes(memory: MemoryService):
    _seed_world(memory)
    memory.remember("school items go to the family calendar")
    block = memory.compact_block()
    assert block.startswith("Confirmed memory:")
    assert "- Rona: gender girl, grade 5, class ה' ספיר, school בראשית, teacher Rinat" in block
    assert "- Itamar: gender boy" in block
    assert "- note: school items go to the family calendar" in block


def test_proposed_note_is_excluded_from_compact_block(memory: MemoryService):
    _seed_world(memory)
    me = memory._ensure_entity(kind="person", name="me")
    memory.add_fact(me.id, "note", value="proposed fact", status="proposed")
    assert "proposed fact" not in memory.compact_block()


# -- entities and aliases ------------------------------------------------------------


def test_entity_crud_and_search(memory: MemoryService):
    created = memory.add_entity("person", "Zohar")
    assert created["name"] == "Zohar"

    assert [row["name"] for row in memory.overview(q="zohar")] == ["Zohar"]
    assert memory.overview(q="nobody-named-this") == []

    renamed = memory.patch_entity(uuid.UUID(created["id"]), name="Zohar A.")
    assert renamed["name"] == "Zohar A."

    memory.delete_entity(uuid.UUID(created["id"]))
    assert memory.overview(q="Zohar") == []


def test_add_fact_requires_an_existing_subject(memory: MemoryService):
    with pytest.raises(KeyError):
        memory.add_fact(uuid.uuid4(), "note", value="orphan")


def test_add_fact_requires_object_or_value(memory: MemoryService):
    me = memory._ensure_entity(kind="person", name="me")
    with pytest.raises(ValueError):
        memory.add_fact(me.id, "note")
