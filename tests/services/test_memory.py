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
def memory_fixture(database: None) -> MemoryService:
    """A clean memory world per test; the session closes with the test.

    Depends on the session-scoped ``database`` fixture so a FRESH test DB
    gets migrated before the first memory test touches it (otherwise the
    whole file errors with UndefinedTable). The session is owned here and
    closed with the test, which keeps the pool from exhausting.
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


def _add_alias(memory: MemoryService, kid_name: str, alias: str) -> None:
    """Give an existing kid a Hebrew alias via the repo's alias writer."""
    from brain.db.models import EntityAlias

    entity = memory._repo.find_entity(kind="person", name=kid_name)
    assert entity is not None
    memory._repo.add_alias(EntityAlias(entity_id=entity.id, alias=alias, lang="he"))
    memory._repo.commit()


def test_named_kids_match_hebrew_aliases(memory: MemoryService):
    """A kid's Hebrew alias resolves the same kid (GO-A follow-up)."""
    _seed_world(memory)
    _add_alias(memory, "Rona", "רונה")
    _add_alias(memory, "Itamar", "איתמר")
    assert memory.resolve_applies_to({"named_kids": ["רונה"]}) == ["Rona"]
    assert memory.resolve_applies_to({"named_kids": ["איתמר"]}) == ["Itamar"]


def test_named_kids_match_case_insensitive_and_prefixed(memory: MemoryService):
    _seed_world(memory)
    _add_alias(memory, "Rona", "רונה")
    # Case-insensitive English ...
    assert memory.resolve_applies_to({"named_kids": ["rona"]}) == ["Rona"]
    # ... and the Hebrew prefix run (לרונה = 'for Rona').
    assert memory.resolve_applies_to({"named_kids": ["לרונה"]}) == ["Rona"]


def test_named_kids_name_and_alias_do_not_double_count(memory: MemoryService):
    _seed_world(memory)
    _add_alias(memory, "Rona", "רונה")
    assert memory.resolve_applies_to({"named_kids": ["Rona", "רונה"]}) == ["Rona"]


def test_named_kids_unknown_name_stays_unknown_with_aliases_present(memory: MemoryService):
    _seed_world(memory)
    _add_alias(memory, "Rona", "רונה")
    assert memory.resolve_applies_to({"named_kids": ["עלמה"]}) == ["unknown"]


def test_aliases_do_not_change_structured_branches(memory: MemoryService):
    _seed_world(memory)
    _add_alias(memory, "Rona", "רונה")
    _add_alias(memory, "Itamar", "איתמר")
    assert (
        memory.resolve_applies_to({"school": "בראשית", "grades": "5-6"}) == ["Rona"]
    )
    assert (
        memory.resolve_applies_to({"school": "בראשית", "gender": "boys"})
        == ["Itamar"]
    )
    assert memory.resolve_applies_to({"school": "בראשית"}) == ["Rona", "Itamar"]


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


# -- channels (WhatsApp baseline: link a channel to its audience) -----------------


def _seed_channel(
    memory: MemoryService,
    name: str,
    kind: str,
    applies_to: str | None,
    *,
    confirmed: bool = True,
) -> dict:
    """Create a channel entity + its facts the way seed-channel will."""
    channel = memory._ensure_entity(kind="channel", name=name)
    memory.add_fact(channel.id, "channel_kind", value=kind)
    fact = None
    if applies_to is not None:
        fact = memory.add_fact(
            channel.id,
            "applies_to",
            value=applies_to,
            status="confirmed" if confirmed else "proposed",
        )
    return {"id": channel.id, "fact": fact}


def test_channel_maps_to_its_kid(memory: MemoryService):
    _seed_world(memory)
    _seed_channel(memory, "הורים כיתה ב' ברקת", "parents", "Itamar")
    assert memory.resolve_applies_to({"channel": "הורים כיתה ב' ברקת"}) == ["Itamar"]


def test_unknown_channel_is_unknown(memory: MemoryService):
    _seed_world(memory)
    assert memory.resolve_applies_to({"channel": "לא קיים"}) == ["unknown"]


def test_family_channel_resolves_all_confirmed_kids_plus_me_at_query_time(
    memory: MemoryService,
):
    _seed_world(memory)
    _seed_channel(memory, "הודעות בית ספר", "parents", "family")
    # kids sorted by name (deterministic), then 'me' — resolved at query time
    assert memory.resolve_applies_to({"channel": "הודעות בית ספר"}) == [
        "Itamar",
        "Rona",
        "me",
    ]
    # a kid added later is included without touching the channel
    memory.set_kid("Noa", gender="girl", school="בראשית", grade="4", class_="ג' ע", teacher="T")
    assert memory.resolve_applies_to({"channel": "הודעות בית ספר"}) == [
        "Itamar",
        "Noa",
        "Rona",
        "me",
    ]


def test_channel_without_applies_to_fact_is_unknown(memory: MemoryService):
    _seed_world(memory)
    _seed_channel(memory, "ערוץ ספורט", "club", None)
    assert memory.resolve_applies_to({"channel": "ערוץ ספורט"}) == ["unknown"]


def test_unconfirmed_applies_to_fact_is_unknown(memory: MemoryService):
    """Only CONFIRMED memory drives resolution (delta item 31d)."""
    _seed_world(memory)
    _seed_channel(memory, "הורים שכבה ה", "parents", "Rona", confirmed=False)
    assert memory.resolve_applies_to({"channel": "הורים שכבה ה"}) == ["unknown"]


# -- seed-channel CLI (values handed over at seed time, nothing hard-coded) -------


def test_seed_channel_cli_roundtrip(memory: MemoryService, capsys):
    """The CLI path produces a channel the resolver can answer from."""
    _seed_world(memory)
    from brain.cli import main

    code = main(
        [
            "memory",
            "seed-channel",
            "--name",
            "וואטסאפ הורי שכבת ה",
            "--kind",
            "parents",
            "--applies-to",
            "Rona",
        ]
    )
    assert code == 0
    assert (
        memory.resolve_applies_to({"channel": "וואטסאפ הורי שכבת ה"}) == ["Rona"]
    )
    out = capsys.readouterr().out
    assert "seeded channel" in out


# --- review round 1 fixes ---------------------------------------------------------


def test_set_kid_rejects_invalid_gender(memory: MemoryService):
    """'Girl'/'female' must fail loudly, not silently mis-resolve audiences."""
    with pytest.raises(ValueError):
        memory.set_kid("X", gender="Girl")
    with pytest.raises(ValueError):
        memory.set_kid("X", gender="female")


def test_set_channel_rejects_unknown_applies_to(memory: MemoryService):
    with pytest.raises(ValueError):
        memory.set_channel("ערוץ", kind="parents", applies_to="rona")  # case matters
    with pytest.raises(ValueError):
        memory.set_channel("ערוץ", kind="parents", applies_to="everyone")


def test_set_channel_reseeds_versioned_not_stacked(memory: MemoryService):
    _seed_world(memory)
    memory.set_channel("ערוץ הורים", kind="parents", applies_to="Itamar")
    assert memory.resolve_applies_to({"channel": "ערוץ הורים"}) == ["Itamar"]

    memory.set_channel("ערוץ הורים", kind="parents", applies_to="family")
    assert memory.resolve_applies_to({"channel": "ערוץ הורים"}) == [
        "Itamar",
        "Rona",
        "me",
    ]
    # history shows 2 versions for applies_to
    channel = memory._repo.find_entity(kind="channel", name="ערוץ הורים")
    versions = [
        f
        for f in memory._repo.facts_about(channel.id)
        if f.predicate == "applies_to"
    ]
    assert [v.value for v in versions] == ["Itamar", "family"]
    assert versions[1].supersedes_id == versions[0].id

    # re-seeding the same value adds no row
    before = len(
        [f for f in memory._repo.facts_about(channel.id) if f.predicate == "applies_to"]
    )
    memory.set_channel("ערוץ הורים", kind="parents", applies_to="family")
    after = len(
        [f for f in memory._repo.facts_about(channel.id) if f.predicate == "applies_to"]
    )
    assert before == after


def test_seed_channel_cli_invalid_value_fails_loudly(memory: MemoryService, capsys):
    from brain.cli import main

    code = main(
        [
            "memory",
            "seed-channel",
            "--name",
            "ערוץ",
            "--kind",
            "parents",
            "--applies-to",
            "rona",
        ]
    )
    assert code == 1
    assert "applies_to must be" in capsys.readouterr().err


def test_seed_kid_cli_invalid_gender_fails_loudly(memory: MemoryService, capsys):
    from brain.cli import main

    code = main(
        [
            "memory",
            "seed-kid",
            "--name",
            "X",
            "--gender",
            "female",
            "--school",
            "s",
            "--grade",
            "1",
            "--class",
            "c",
            "--teacher",
            "t",
        ]
    )
    assert code == 1
    assert "gender must be" in capsys.readouterr().err


def test_hebrew_teen_grades(memory: MemoryService):
    """ל=30 and מ=40 in Hebrew numerals — grades 11/12 are יא/יב only."""
    _seed_world(memory)
    memory.set_kid("Noa", gender="girl", school="בראשית", grade="11", class_="יא א", teacher="T")
    assert memory.resolve_applies_to({"school": "בראשית", "grades": "יא-יב"}) == ["Noa"]
    assert memory.resolve_applies_to({"school": "בראשית", "grades": "יג"}) == [
        "nobody"
    ]  # יג parses as 13 (a real value) — no kid in it is a KNOWN mismatch


def test_grade_spellings_from_real_emails(memory: MemoryService):
    """Geresh, gershayim, maqaf and dashes all parse (textnorm first)."""
    _seed_world(memory)
    for spelling in ("ה'", "ה׳", "ה־ו", "ה–ו", "ה-ו"):
        assert memory.resolve_applies_to({"school": "בראשית", "grades": spelling}) == [
            "Rona"
        ], spelling


def test_patch_closed_fact_raises(memory: MemoryService):
    """Editing a superseded or soft-deleted fact must not resurrect it."""
    _seed_world(memory)
    old = _find_fact(memory, "grade", "5")
    new_version = memory.patch_fact(old.id, value="6")
    with pytest.raises(MemoryError):
        memory.patch_fact(old.id, value="7")  # superseded -> closed
    memory.delete_fact(uuid.UUID(new_version["id"]))
    with pytest.raises(MemoryError):
        memory.patch_fact(uuid.UUID(new_version["id"]), value="7")  # rejected -> closed


def test_delete_entity_refused_while_current_facts_exist(memory: MemoryService):
    _seed_world(memory)
    rona = memory._repo.find_entity(kind="person", name="Rona")
    with pytest.raises(MemoryError):
        memory.delete_entity(rona.id)  # has current facts
    for fact in memory._repo.current_facts(subject_ids=[rona.id]):
        memory.delete_fact(fact.id)
    memory.delete_entity(rona.id)  # now allowed (history rows cascade)
    assert memory._repo.find_entity(kind="person", name="Rona") is None


# --- follow-up cleanup PR (review of PR #28 round) ---------------------------------


def test_grade_range_with_quotes_on_both_ends(memory: MemoryService):
    """'ה'-ו'' and 'ה׳–ו׳' — quotes on both ends of the range still parse."""
    _seed_world(memory)
    for spelling in ("ה'-ו'", "ה׳–ו׳"):
        assert memory.resolve_applies_to(
            {"school": "בראשית", "grades": spelling}
        ) == ["Rona"], spelling


def test_bare_yod_is_grade_ten(memory: MemoryService):
    """Bare 'י' (grade 10) and 'י'' (with geresh) parse again."""
    _seed_world(memory)
    memory.set_kid("Noa", gender="girl", school="בראשית", grade="10", class_="י א", teacher="T")
    assert memory.resolve_applies_to({"school": "בראשית", "grades": "י"}) == ["Noa"]
    assert memory.resolve_applies_to({"school": "בראשית", "grades": "י'"}) == ["Noa"]


def test_set_channel_rejects_invalid_kind(memory: MemoryService):
    with pytest.raises(ValueError):
        memory.set_channel("ערוץ", kind="Teachers", applies_to="me")
    with pytest.raises(ValueError):
        memory.set_channel("ערוץ", kind="family", applies_to="me")
    # the valid set still works
    memory.set_channel("ערוץ", kind="teacher", applies_to="me")


# -- A2 run-#1 fix 1: normalized label comparison -------------------------------


def test_class_label_variants_all_match(memory: MemoryService):
    """ה' ספיר (memory) == ה׳ ספיר / ה ספיר / כיתה ה' ספיר / ה-ספיר (text)."""
    _seed_world(memory)
    for variant in ("ה׳ ספיר", "ה ספיר", "כיתה ה' ספיר", "ה-ספיר", "ה' ספיר"):
        assert memory.resolve_applies_to({"class": variant}) == ["Rona"], variant


def test_school_label_variant_still_matches(memory: MemoryService):
    _seed_world(memory)
    assert memory.resolve_applies_to({"school": "בָּרָאשִׁית"}) == ["Rona", "Itamar"]


def test_different_class_still_incompatible(memory: MemoryService):
    _seed_world(memory)
    assert memory.resolve_applies_to({"class": "ו' אלון"}) == ["nobody"]


# -- A2 run-#1 fix 2: sender -> default audience (R1) ---------------------------


def test_sender_matching_teacher_is_the_default_audience(memory: MemoryService):
    _seed_world(memory)
    assert memory.default_audience_for_sender("Rinat <rinat@bereshit.edu>") == ["Rona"]
    assert memory.default_audience_for_sender("Maayan") == ["Itamar"]
    assert memory.default_audience_for_sender("דואר כללי <office@edu>") is None
    assert memory.default_audience_for_sender(None) is None
    assert memory.default_audience_for_sender("") is None


def test_sender_default_uses_whole_word_teacher_matching(memory: MemoryService):
    """Review round 2, bug 2: raw containment matched BOTH ways — a parent
    'Rinat Levi' whose first name equals the teacher's, a word merely
    CONTAINING the teacher name, and a short sender inside a long teacher
    name all defaulted to the kid. The shared whole-word matcher
    (triage.mentions_name, one direction) fixes all three."""
    _seed_world(memory)
    # whole-word: a parent whose first name equals the teacher's still matches
    assert memory.default_audience_for_sender("Rinat Levi <rinat.levi@gmail.com>") == ["Rona"]
    # a longer word merely containing the teacher name does not
    assert memory.default_audience_for_sender("Rinatech Ltd <office@rinatech.co>") is None
    # a short sender string inside a longer teacher name does not
    assert memory.default_audience_for_sender("Rin") is None


def test_sender_default_hebrew_teacher_prefix_rule(memory: MemoryService):
    """Review round 2, bug 2 (Hebrew): mentions_name allows attached Hebrew
    prepositions (והבכלמש) before the name but not arbitrary letters —
    'קרינת' is NOT the teacher 'רינת'."""
    _seed_world(memory)
    memory.set_kid(
        "Noa",
        gender="girl",
        school="בראשית",
        grade="3",
        class_="ג' כוכב",
        teacher="רינת",
    )
    assert memory.default_audience_for_sender("רינת לוי") == ["Noa"]
    # Hebrew display name + an address containing the Latin 'rinat': BOTH
    # teacher facts match — the acknowledged residual (a first-name-only
    # teacher fact can match a same-named parent; results are proposals,
    # D2 Keep/Fix/Drop)
    assert memory.default_audience_for_sender("רינת לוי <rinat.levi@gmail.com>") == [
        "Noa",
        "Rona",
    ]
    assert memory.default_audience_for_sender("משפחת קרינת") is None
