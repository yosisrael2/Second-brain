"""Bot /profile and /remember tests against the REAL MemoryService (PR 0 core).

DB-backed (brain_w5_test via conftest; memory tables wiped per test). There is
deliberately NO fake for the memory service here: a fake that disagreed with
the real contract is exactly how the overview-shape crash slipped through
review round 0.
"""

from contextlib import contextmanager

from sqlalchemy import text
from tests.conftest import assert_test_database
from tests.fakes import FakeRepository, StubClassifier

from brain.db.session import SessionLocal, engine
from brain.repositories.memory import MemoryRepository
from brain.services.ingest import IngestService
from brain.services.memory import MemoryService
from brain.telegram_bot import BotServices, TelegramBot

_ALLOWED = frozenset({111})


def _wipe_memory() -> None:
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM memory_facts"))
        conn.execute(text("DELETE FROM entity_aliases"))
        conn.execute(text("DELETE FROM entities"))


def _core() -> TelegramBot:
    """A TelegramBot whose factory serves the REAL MemoryService per update."""

    @contextmanager
    def factory():
        repo = FakeRepository()
        session = SessionLocal()
        try:
            yield BotServices(
                ingest=IngestService(repo, StubClassifier(), indexer=None),
                items=None,
                search=None,
                ask=None,
                memory=MemoryService(MemoryRepository(session)),
            )
        finally:
            session.close()

    return TelegramBot(services_factory=factory, allowed_ids=_ALLOWED, timezone=None)


def _seed_family(memory: MemoryService) -> None:
    """One kid (user-confirmed seed path) + one free-text note on 'me'."""
    memory.set_kid(
        "Romi",
        gender="girl",
        school="בית הספר הגפן",
        grade="5",
        class_="ה' ברוש",
        teacher="Tamar",
    )
    memory.remember("Ilan plays basketball")


def test_profile_shows_seeded_kid_and_note(memory_db) -> None:
    core = _core()
    with _core()._services_factory() as services:  # noqa: SLF001 - seeding
        _seed_family(services.memory)

    reply = core.route("/profile", 111)

    assert "Profile" in reply.text
    assert "Romi" in reply.text and "girl" in reply.text and "Tamar" in reply.text
    assert "ה' ברוש" in reply.text
    assert "Ilan plays basketball" in reply.text
    # every ✖ button carries the number of its fact line (must-fix 2)
    buttons = [label for row in (reply.keyboard or []) for (label, _) in row]
    assert buttons, "profile must offer one ✖ per fact line"
    for n, label in enumerate(buttons, start=1):
        assert label == f"✖ {n}"
        assert f"{n}." in reply.text, f"fact line {n} must carry its button number"


def test_profile_remove_round_trip(memory_db) -> None:
    core = _core()
    with _core()._services_factory() as services:  # noqa: SLF001 - seeding
        memory = services.memory
        _seed_family(memory)
        facts_before = {
            f["id"] for e in memory.overview(status="confirmed") for f in e["facts"]
        }

    first = core.route("/profile", 111)
    _, callback = first.keyboard[0][0]  # "✖ 1"

    second = core.handle_callback(callback)

    assert second.edit is True
    assert "Removed" in second.text
    with _core()._services_factory() as services:  # noqa: SLF001 - verifying
        memory = services.memory
        facts_after = {
            f["id"] for e in memory.overview(status="confirmed") for f in e["facts"]
        }
    assert len(facts_after) == len(facts_before) - 1
    # and the card still renders (edit-in-place)
    assert "Profile" in second.text


def test_profile_remove_already_removed_is_graceful(memory_db) -> None:
    core = _core()
    with _core()._services_factory() as services:  # noqa: SLF001 - seeding
        _seed_family(services.memory)

    first = core.route("/profile", 111)
    _, callback = first.keyboard[0][0]

    assert core.handle_callback(callback).edit is True
    # second press on the stale button: the fact is now closed ->
    # MemoryService.delete_fact raises MemoryError (not KeyError)
    second = core.handle_callback(callback)
    assert "already removed" in second.toast


def test_remember_stores_confirmed_note_on_me(memory_db) -> None:
    core = _core()

    reply = core.route("/remember <b>raw & text</b>", 111)

    assert "🧠" in reply.text
    assert "&lt;b&gt;raw &amp; text&lt;/b&gt;" in reply.text  # escaped echo
    with _core()._services_factory() as services:  # noqa: SLF001 - verifying
        notes = [
            f
            for e in services.memory.overview(status="confirmed")
            if e["name"] == "me"
            for f in e["facts"]
            if f["predicate"] == "note"
        ]
    assert [n["value"] for n in notes] == ["<b>raw & text</b>"]  # raw in DB


def test_remember_without_text_shows_usage(memory_db) -> None:
    reply = _core().route("/remember", 111)

    assert "Usage" in reply.text


def test_profile_and_remember_without_memory_service_are_graceful() -> None:
    @contextmanager
    def factory():
        yield BotServices(
            ingest=IngestService(FakeRepository(), StubClassifier(), indexer=None),
            items=None,
            search=None,
            ask=None,
            memory=None,
        )

    core = TelegramBot(services_factory=factory, allowed_ids=_ALLOWED, timezone=None)

    profile = core.route("/profile", 111)
    remember = core.route("/remember x", 111)

    assert "not installed" in profile.text
    assert "not installed" in remember.text


def test_remove_buttons_survive_registry_ttl_gracefully(memory_db) -> None:
    core = _core()
    with _core()._services_factory() as services:  # noqa: SLF001 - seeding
        _seed_family(services.memory)

    first = core.route("/profile", 111)
    _, callback = first.keyboard[0][0]
    core.registry._entries.clear()  # noqa: SLF001 - force-expire the token

    reply = core.handle_callback(callback)

    assert "expired" in reply.toast


# --- bot language (a memory-layer fact on 'me') ---------------------------------


def test_language_defaults_to_english_and_is_set_by_command(memory_db) -> None:
    core = _core()
    with core._services_factory() as services:  # noqa: SLF001 - reading
        assert services.memory.user_language() == "en"

    reply = core.route("/language he", 111)

    assert reply.text == "🌐 השפה הוגדרה לעברית."
    with core._services_factory() as services:  # noqa: SLF001 - reading
        assert services.memory.user_language() == "he"


def test_language_change_supersedes_one_current_fact(memory_db) -> None:
    core = _core()
    core.route("/language he", 111)
    core.route("/language en", 111)
    core.route("/language en", 111)  # unchanged: no extra history row

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT value, valid_to IS NULL FROM memory_facts "
                "WHERE predicate = 'language' ORDER BY valid_from"
            )
        ).all()
    assert [tuple(r) for r in rows] == [("he", False), ("en", True)]


def test_language_command_rejects_unknown_codes(memory_db) -> None:
    core = _core()
    reply = core.route("/language fr", 111)
    assert reply.text.startswith("Usage: /language he|en")
    with core._services_factory() as services:  # noqa: SLF001 - reading
        assert services.memory.user_language() == "en"


def test_display_names_use_the_hebrew_alias(memory_db) -> None:
    core = _core()
    with core._services_factory() as services:  # noqa: SLF001 - seeding
        _seed_family(services.memory)
        romi = services.memory._repo.find_entity(kind="person", name="Romi")  # noqa: SLF001
        services.memory.add_alias(romi.id, "Roni")
        services.memory.add_alias(romi.id, "רומי")
        assert services.memory.display_names("he") == {"Romi": "רומי"}
        assert services.memory.display_names("en") == {}
