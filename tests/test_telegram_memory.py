"""Bot /profile and /remember tests (memory layer, PR 0b part C).

Runs fully offline: the TelegramBot core is PTB-agnostic; the memory service
is a fake implementing the PR 0 contract (overview/remember/delete_fact).
"""

import uuid
from contextlib import contextmanager
from typing import Any

from tests.fakes import FakeRepository, StubClassifier

from brain.services.ingest import IngestService
from brain.telegram_bot import BotServices, TelegramBot

KID_FACTS = [
    {"id": str(uuid.uuid4()), "predicate": "gender", "value": "girl"},
    {"id": str(uuid.uuid4()), "predicate": "class", "value": "ה' ספיר"},
    {"id": str(uuid.uuid4()), "predicate": "grade", "value": "5"},
    {"id": str(uuid.uuid4()), "predicate": "teacher", "value": "Rinat"},
]
ME_FACTS = [
    {"id": str(uuid.uuid4()), "predicate": "note", "value": "Itamar plays basketball"},
]


def _entity(name: str, facts: list[dict]) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "kind": "person",
        "name": name,
        "aliases": [],
        "facts": facts,
    }


class FakeMemory:
    """The PR 0 MemoryService contract, scripted."""

    def __init__(self, entities: list[dict] | None = None) -> None:
        self.entities = entities if entities is not None else []
        self.deleted: list[uuid.UUID] = []
        self.remembered: list[str] = []

    def overview(self, *, status: str = "confirmed", q: str | None = None) -> dict:
        return {"entities": self.entities}

    def remember(self, text: str) -> dict:
        self.remembered.append(text)
        return {"id": str(uuid.uuid4()), "predicate": "note", "value": text}

    def delete_fact(self, fact_id: uuid.UUID) -> None:
        self.deleted.append(fact_id)


class _ExplodingMemory:
    """A memory service whose delete_fact loses rows (already removed)."""

    class FactNotFound(Exception):
        pass

    def __init__(self, entities: list[dict] | None = None) -> None:
        self.entities = entities or []

    def overview(self, *, status: str = "confirmed", q: str | None = None) -> dict:
        return {"entities": self.entities}

    def remember(self, text: str) -> dict:
        return {"id": str(uuid.uuid4()), "predicate": "note", "value": text}

    def delete_fact(self, fact_id: uuid.UUID) -> None:
        raise _ExplodingMemory.FactNotFound(str(fact_id))


def _core(memory: Any) -> TelegramBot:
    """A TelegramBot whose factory serves the given memory service."""

    @contextmanager
    def factory():
        repo = FakeRepository()
        yield BotServices(
            ingest=IngestService(repo, StubClassifier(), indexer=None),
            items=None,
            search=None,
            ask=None,
            memory=memory,
        )

    return TelegramBot(services_factory=factory, allowed_ids=ALLOWED, timezone=None)


def test_remember_stores_confirmed_fact() -> None:
    memory = FakeMemory()
    core = _core(memory)

    reply = core.route("/remember Itamar plays basketball", 111)

    assert memory.remembered == ["Itamar plays basketball"]
    assert "🧠" in reply.text
    assert "Remembered" in reply.text
    assert "Itamar plays basketball" in reply.text
    assert reply.keyboard is None


def test_remember_escapes_html_in_echoed_text() -> None:
    core = _core(FakeMemory())

    reply = core.route("/remember <b>secret</b> & facts", 111)

    assert "<b>secret</b>" not in reply.text  # raw tags must not pass through
    assert "&lt;b&gt;secret&lt;/b&gt;" in reply.text
    assert "&amp;" in reply.text


def test_remember_without_text_shows_usage() -> None:
    core = _core(FakeMemory())

    reply = core.route("/remember", 111)

    assert "Usage" in reply.text
    assert reply.keyboard is None


def test_profile_empty_gives_a_hint() -> None:
    core = _core(FakeMemory(entities=[]))

    reply = core.route("/profile", 111)

    assert "Profile is empty" in reply.text
    assert reply.keyboard is None


def test_profile_lists_kids_first_with_facts_and_remove_buttons() -> None:
    kid = _entity("Rona", KID_FACTS)
    me = _entity("me", ME_FACTS)
    memory = FakeMemory(entities=[me, kid])  # 'me' first on purpose
    core = _core(memory)

    reply = core.route("/profile", 111)

    assert "Profile" in reply.text
    # kid section before the remembered section, facts escaped + present
    assert reply.text.index("Rona") < reply.text.index("Remembered")
    assert "girl" in reply.text and "ה' ספיר" in reply.text
    assert "Rinat" in reply.text
    assert "Itamar plays basketball" in reply.text
    # one ✖ button per fact (5), rows of 4
    flat = [label for row in (reply.keyboard or []) for (label, _) in row]
    assert flat == ["✖ 1", "✖ 2", "✖ 3", "✖ 4", "✖ 5"]


def test_profile_remove_button_deletes_the_matching_fact() -> None:
    kid = _entity("Rona", KID_FACTS)
    me = _entity("me", ME_FACTS)
    memory = FakeMemory(entities=[me, kid])
    core = _core(memory)

    first = core.route("/profile", 111)
    _, callback = first.keyboard[0][0]  # "✖ 1" -> m:<token>:0

    second = core.handle_callback(callback)

    assert len(memory.deleted) == 1
    deleted = memory.deleted[0]
    # line 1 belongs to the FIRST rendered fact line: the kid block (kids first)
    assert str(deleted) == KID_FACTS[0]["id"]
    assert second.edit is True
    assert "Removed" in second.text
    assert "Profile" in second.text  # re-rendered card


def test_profile_remove_twice_is_graceful() -> None:
    kid = _entity("Rona", KID_FACTS)
    memory = _ExplodingMemory(entities=[kid])
    core = _core(memory)

    first = core.route("/profile", 111)
    _, callback = first.keyboard[0][0]

    second = core.handle_callback(callback)

    assert "already removed" in second.text
    assert second.edit is False


def test_profile_and_remember_without_memory_service_are_graceful() -> None:
    core = _core(memory=None)

    profile = core.route("/profile", 111)
    remember = core.route("/remember x", 111)

    assert "not installed" in profile.text
    assert "not installed" in remember.text


def test_remove_buttons_survive_registry_ttl_gracefully() -> None:
    core = _core(FakeMemory(entities=[_entity("Rona", KID_FACTS)]))

    first = core.route("/profile", 111)
    _, callback = first.keyboard[0][0]
    core.registry._entries.clear()  # force-expire the pending token

    reply = core.handle_callback(callback)

    assert "expired" in reply.text
