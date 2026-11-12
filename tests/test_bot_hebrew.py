"""The whole bot speaks Hebrew when the user's language is Hebrew.

A guard over every main command, capture and button: with ``language=he``
(the memory-layer fact behind ``/language he``) no reply text or button
label carries an English word — only the allowlisted command names, the
language codes and the HTML tags. Plus one test per fixed message class
(direction isolates, plural forms, profile labels, the Fix flow words,
relation errors, /status, the « Back button, one language read per update).

DB-backed over the *_test database with the REAL TelegramBot, ItemsService,
IngestService and MemoryService; search/ask/attachments are local fakes. No
Telegram, no LLM, no network.
"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from brain import i18n
from brain import telegram_bot as tb
from brain.db.enums import ItemStatus, ItemType
from brain.db.models import ItemRelation, ItemSource, MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.repositories.memory import MemoryRepository
from brain.services.ask import AskResult
from brain.services.ingest import IngestService
from brain.services.items import ItemsService
from brain.services.memory import MemoryService
from brain.services.proposals import FixError, verdict_marker
from brain.services.relations import RelationsService
from brain.services.scheduled_tasks import ScheduledTaskStore
from brain.services.search import SearchHit
from conftest import assert_test_database

TZ = ZoneInfo("Asia/Jerusalem")
USER = 111

# What may stay Latin in a Hebrew reply: command names, the language codes
# of /language, the HTML tags of the card markup, and direction-isolated data.
_ALLOWED = re.compile(
    r"/(?:start|help|status|search|ask|review|recent|profile|remember|schedules|language)\b"
    r"|\b(?:he|en)\b"
    r"|</?[bs]>|<blockquote expandable>|</blockquote>"
    r"|&(?:lt|gt|amp);"
    # user DATA the bot isolates on purpose (a file name, an English subject)
    r"|\u2068[^\u2069]*\u2069"
)
_LATIN = re.compile("[A-Za-z]")


def _wipe() -> None:
    assert_test_database(str(engine.url))
    with engine.begin() as conn:
        for table in (
            "item_relations",
            "item_sources",
            "memory_items",
            "scheduled_task_runs",
            "scheduled_tasks",
        ):
            conn.execute(text(f"DELETE FROM {table}"))


@pytest.fixture(name="world")
def world_fixture(memory_db: None):
    _wipe()
    yield
    _wipe()


class _NoIndexer:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Classifier:
    def classify(self, text: str) -> dict:
        return {
            "type": ItemType.NOTE,
            "project": None,
            "importance": 3,
            "actionability": "later",
            "confidence": 0.5,  # -> needs_review: the 🔎 line is rendered
        }


class _Search:
    def __init__(self, session) -> None:
        self._session = session

    def search(self, query, *, limit, offset=0, extra_items=()):
        rows = self._session.scalars(
            select(MemoryItem)
            .where(MemoryItem.raw_text.ilike(f"%{query}%"))
            .order_by(MemoryItem.created_at.desc())
            .limit(limit)
        ).all()
        return [SearchHit(item=row, score=0.5) for row in rows]


class _Ask:
    def __init__(self, session) -> None:
        self._session = session

    def ask(self, question):
        rows = self._session.scalars(
            select(MemoryItem).where(MemoryItem.child_state.is_(None)).limit(2)
        ).all()
        ids = [str(row.id) for row in rows]
        answer = "יש אסיפה " + " ".join(f"[{i}]" for i in ids)
        return AskResult(
            answer=answer,
            cited_ids=ids,
            sources=[SearchHit(item=row, score=0.9) for row in rows],
            model="some-model",
        )


@dataclass
class _Attachment:
    filename: str
    text: str


class _Attachments:
    def get(self, attachment_id):
        return _Attachment(filename="תעודה.pdf", text="ציונים")

    def own_item(self, attachment_id):
        return None


def _core() -> tb.TelegramBot:
    @contextmanager
    def factory():
        session = SessionLocal()
        try:
            repo = ItemsRepository(session)
            yield tb.BotServices(
                ingest=IngestService(repo, _Classifier(), _NoIndexer()),
                items=ItemsService(repo, _NoIndexer()),
                search=_Search(session),
                ask=_Ask(session),
                memory=MemoryService(MemoryRepository(session)),
                attachments=_Attachments(),
            )
        finally:
            session.close()

    return tb.TelegramBot(services_factory=factory, allowed_ids=frozenset({USER}), timezone=TZ)


def _seed_family(core: tb.TelegramBot, lang: str = "he") -> None:
    with core._services_factory() as services:  # noqa: SLF001 - seeding
        memory = services.memory
        memory.set_kid("Noa", gender="girl", school="הדקל", grade="3", class_="ג׳ תכלת",
                       teacher="שירה")
        memory.set_kid("Dan", gender="boy", school="הדקל", grade="1", class_="א׳ כתום",
                       teacher="אורית")
        by_name = {e["name"]: e for e in memory.overview(status="confirmed", q=None)}
        memory.add_alias(uuid.UUID(str(by_name["Noa"]["id"])), "נועה")
        memory.add_alias(uuid.UUID(str(by_name["Dan"]["id"])), "דן")
        memory.add_fact(
            uuid.UUID(str(by_name["Noa"]["id"])),
            "connection:sibling",
            object_id=uuid.UUID(str(by_name["Dan"]["id"])),
        )
        memory.remember("דן משחק כדורסל")
        memory.set_user_language(lang)


def _proposal_source() -> MemoryItem:
    """A source with two proposed children + one recon CONFLICT child."""
    with SessionLocal() as session:
        repo = ItemsRepository(session)
        source = repo.add(MemoryItem(raw_text="מייל מבית הספר: אסיפת הורים"))
        existing = repo.add(MemoryItem(raw_text="חוג שחייה ביום ראשון 16:00"))
        repo.commit()
        when = datetime.now(tz=UTC) + timedelta(hours=3)
        children = [
            repo.add(MemoryItem(raw_text="אסיפת הורים", type=ItemType.EVENT,
                                child_state="proposed", occurred_at=when)),
            repo.add(MemoryItem(raw_text="להחזיר אישור חתום", type=ItemType.TASK,
                                child_state="proposed", occurred_at=when)),
            repo.add(MemoryItem(raw_text="חוג שחייה ביום ראשון 17:30", child_state="proposed",
                                extraction_meta={"recon_verdict": "conflict",
                                                 "recon_fields": {"time": ["16:00", "17:30"]}})),
        ]
        session.flush()
        for child in children:
            session.add(ItemSource(child_id=child.id, source_id=source.id, quote=child.raw_text))
        session.add(ItemRelation(from_id=children[2].id, to_id=existing.id, kind="conflicts",
                                 provenance={"how": "recon"}))
        repo.commit()
        session.expunge(source)
        return source


def _find(reply: tb.Reply, prefix: str, nth: int = 0) -> str:
    found = [data for row in reply.keyboard or [] for _, data in row
             if data.split(":")[0] == prefix]
    assert len(found) > nth, f"no {prefix} button in {reply.keyboard}"
    return found[nth]


def _english_left(reply: tb.Reply) -> list[str]:
    """Latin words left in a reply's text, toast and button labels (allowlist removed)."""
    labels = [label for row in reply.keyboard or [] for label, _ in row]
    chunks = [reply.text, reply.toast or "", *labels]
    leftovers = []
    for chunk in chunks:
        stripped = _ALLOWED.sub("", chunk)
        if _LATIN.search(stripped):
            leftovers.append(chunk)
    return leftovers


def _drive_everything(core: tb.TelegramBot, monkeypatch) -> list[tuple[str, tb.Reply]]:
    """Every main command, capture and button, in order; (step, reply) pairs."""
    monkeypatch.setattr(
        tb, "_ops_status_entries",
        lambda: [("ok", "postgres", "ok"), ("disabled", "whatsapp", "off")],
    )
    out: list[tuple[str, tb.Reply]] = []

    def step(name: str, reply):
        for one in reply if isinstance(reply, list) else [reply]:
            out.append((name, one[1] if isinstance(one, tuple) else one))
        return reply

    for command in ("/start", "/help", "/status", "/search", "/search אין-כזה", "/ask",
                    "/language", "/remember", "/schedules", "/recent", "/review"):
        step(command, core.route(command, USER))
    note = step("capture", core.route("לקנות חלב ולחם", USER))
    step("capture again", core.route("לקנות חלב ולחם", USER))
    step("forwarded", core.route("הורים יקרים, מחר טיול", USER,
                                 forward={"sender": "שירה", "date": datetime.now(tz=UTC)}))
    thread = step("whatsapp", core.route(
        "[08/11/2026, 09:12:01] אמא: מתי האסיפה?\n[08/11/2026, 09:13:44] שירה: ביום רביעי", USER))
    step("whatsapp join", core.handle_callback(_find(thread, "w1"), USER))
    menu = step("type menu", core.handle_callback(_find(note, "t"), USER))
    step("type back", core.handle_callback(_find(menu, "b"), USER))
    step("type apply", core.handle_callback(_find(menu, "ty"), USER))
    step("file", core.handle_callback(_find(note, "f"), USER))
    with SessionLocal() as session:
        repo = ItemsRepository(session)
        for body in ("הודעה ראשונה לבדיקה", "הודעה שנייה לבדיקה"):
            repo.add(MemoryItem(raw_text=body, status=ItemStatus.INBOX, actionability="now",
                                meta={"subject": "טיול שנתי", "location": "בית הספר"}))
        repo.commit()
    review = step("/review", core.route("/review", USER))
    step("review next", core.handle_callback(_find(review, "n"), USER))
    step("review delete", core.handle_callback(_find(review, "d"), USER))
    step("undo", core.handle_callback(_find(note, "u"), USER))
    step("/search", core.route("/search טיול", USER))
    answer = step("question", core.route("מתי הטיול?", USER))
    step("open", core.handle_callback(_find(answer, "o"), USER))
    step("/recent", core.route("/recent", USER))
    step("/remember", core.route("/remember נועה אוהבת ציור", USER))
    profile = step("/profile", core.route("/profile", USER))
    step("profile remove", core.handle_callback(_find(profile, "m"), USER))
    with SessionLocal() as session:
        ScheduledTaskStore(session).create(name="סיכום בוקר", prompt="מה יש היום?",
                                           schedule="0 7 * * 0-4", timezone="Asia/Jerusalem")
    schedules = step("/schedules", core.route("/schedules", USER))
    step("schedule run", core.handle_callback(_find(schedules, "sr"), USER))
    step("schedule pause", core.handle_callback(_find(schedules, "st"), USER))
    step("expired", core.handle_callback("f:deadbeef", USER))
    step("unknown", core.handle_callback("zz:1", USER))
    step("document", core.capture_document("invite.ics", "אסיפת הורים"))
    step("voice", core.unsupported("Voice and video"))
    step("files", core.unsupported("Files other than .ics"))
    for status in ("done", "unsupported", "too_large", "failed"):
        step(f"attachment {status}", core.attachment_summary(uuid.uuid4(), status))

    source = _proposal_source()
    card = step("proposal card", core._proposal_reply_for(source))  # noqa: SLF001
    step("due ping", core.due_pings())
    card = step("keep", core.handle_callback(_find(card, "pk"), USER))
    step("fix", core.handle_callback(_find(card, "pf"), USER))
    step("fix title", core.route("אסיפת הורים כיתה ה׳", USER))
    step("fix bad date", core.route("מתישהו", USER))
    step("fix cancel", core.route("ביטול", USER))
    card = step("conflict keep existing", core.handle_callback(_find(card, "rd"), USER))
    step("drop", core.handle_callback(_find(card, "px"), USER))
    step("stale card button", core.handle_callback(_find(card, "pk"), USER))
    step("keep all", core.handle_callback(_find(card, "pa"), USER))

    with SessionLocal() as session:
        repo = ItemsRepository(session)
        old = repo.add(MemoryItem(raw_text="ארוחת ערב ביום שישי"))
        new = repo.add(MemoryItem(raw_text="ארוחת ערב עברה לשבת"))
        repo.commit()
        old_id, new_id = old.id, new.id
    RelationsService().apply_supersede(new_id=new_id, old_id=old_id)
    token = core.registry.put(tb.Pending(ids=(new_id, old_id)))
    replaced = step("open replacing item", core.handle_callback(f"o:{token}:0", USER))
    struck = step("open replaced item", core.handle_callback(f"o:{token}:1", USER))
    step("restore", core.handle_callback(_find(struck, "rs"), USER))
    step("undo stale", core.handle_callback(_find(replaced, "ru"), USER))
    return out


def test_every_hebrew_reply_is_free_of_english(world, monkeypatch) -> None:
    core = _core()
    _seed_family(core, "he")

    replies = _drive_everything(core, monkeypatch)

    assert len(replies) > 50
    leftovers = {name: _english_left(reply) for name, reply in replies if _english_left(reply)}
    assert leftovers == {}


def test_english_replies_stay_english(world, monkeypatch) -> None:
    """The same walk in English: nothing Hebrew from the bot's own strings."""
    core = _core()
    _seed_family(core, "en")

    replies = dict(_drive_everything(core, monkeypatch))

    assert replies["/start"].text.startswith("👋 Second Brain bot ready.")
    assert replies["/review"].text.startswith("📥 Inbox (1/")
    assert replies["expired"].toast == (
        "⌛ That button expired (valid 10 minutes) — send the message again."
    )
    assert replies["fix cancel"].text == "✏️ Fix cancelled — nothing changed."  # "ביטול" works too


# --- one test per fixed message class -------------------------------------------


def test_back_button_returns_to_the_card(world) -> None:
    """« Back on the type menu was never routed ("Unknown action")."""
    core = _core()
    card = core.route("a note to retype", USER)
    menu = core.handle_callback(_find(card, "t"), USER)

    back = core.handle_callback(_find(menu, "b"), USER)

    assert "Unknown action" not in back.text
    assert "a note to retype" in back.text and back.edit


def test_hebrew_commands_carry_lrm_so_the_slash_stays_left(world) -> None:
    help_text = i18n.t("he", "help")
    commands = re.findall(r"(.)/(?:search|ask|review|recent|profile|remember|schedules)", help_text)
    assert commands and set(commands) == {"‎"}
    for line in help_text.splitlines():  # every line starts right-to-left
        first = next((ch for ch in line if ch.isalpha()), None)
        assert first is None or not _LATIN.match(first), line


def test_latin_fragments_are_isolated_only_in_hebrew() -> None:
    assert i18n.iso("report.pdf", "he") == "⁨report.pdf⁩"
    assert i18n.iso("תעודה.pdf", "he") == "⁨תעודה.pdf⁩"
    assert i18n.iso("תעודה", "he") == "תעודה"
    assert i18n.iso("report.pdf", "en") == "report.pdf"


def test_hebrew_counts_use_singular_forms() -> None:
    assert i18n.tn("he", "kept_n", 1) == "✅ נשמר פריט אחד."
    assert i18n.tn("he", "kept_n", 3) == "✅ נשמרו 3 פריטים."
    assert i18n.tn("he", "deleted", 1) == "🗑 נמחק."
    assert i18n.tn("en", "deleted", 1) == "🗑 Deleted 1 item(s)."  # English unchanged
    assert i18n.tn("he", "search_header", 1, query="x") == "🔍 מצאתי תוצאה אחת עבור: x"


def test_profile_in_hebrew_labels_names_and_connections(world) -> None:
    core = _core()
    _seed_family(core, "he")

    text_ = core.route("/profile", USER).text

    assert "🧒 <b>נועה</b>" in text_ and "🧒 <b>דן</b>" in text_
    assert "מחנכת: שירה" in text_ and "מין: בת" in text_
    assert "אח/אחות של: דן" in text_  # was "connection:sibling: None"
    assert "language" not in text_ and "שפה" not in text_  # a setting, not a fact
    assert "None" not in text_


def test_profile_adults_are_not_listed_as_kids(world) -> None:
    core = _core()
    with core._services_factory() as services:  # noqa: SLF001
        memory = services.memory
        adult = memory.add_entity("person", "Dana")
        memory.add_fact(uuid.UUID(str(adult["id"])), "gender", value="woman")

    text_ = core.route("/profile", USER).text

    assert "👤 <b>Dana</b>" in text_ and "🧒" not in text_


def test_fix_flow_accepts_hebrew_words(world) -> None:
    assert {"ביטול", "cancel"} <= i18n.CANCEL_WORDS
    assert {"השאר", "keep"} <= i18n.KEEP_WORDS
    core = _core()
    _seed_family(core, "he")
    card = core._proposal_reply_for(_proposal_source())  # noqa: SLF001
    core.handle_callback(_find(card, "pf"), USER)
    core.route("שם חדש", USER)

    done = core.route("השאר", USER)

    assert done.text.startswith("✏️ תוקן: שם חדש")


def test_fix_errors_are_translated_by_code() -> None:
    assert FixError("x", "bad_date").code == "bad_date"
    assert i18n.label("he", "fixerr_bad_date", "x").startswith("לא הבנתי את התאריך")
    assert i18n.label("he", "fixerr_unknown_code", "raw") == "raw"


def test_relation_errors_never_show_ids_in_hebrew(world) -> None:
    from brain.services.relations import RelationsError

    core = _core()
    _seed_family(core, "he")
    rel_id = uuid.uuid4()

    with core._services_factory():  # noqa: SLF001
        message = tb._speaks(lambda self: self._relation_error(  # noqa: SLF001
            RelationsError(f"no such relation: {rel_id}")))(core)

    assert str(rel_id) not in message and message == i18n.t("he", "already_handled")


def test_status_in_hebrew_words(world, monkeypatch) -> None:
    core = _core()
    _seed_family(core, "he")
    monkeypatch.setattr(tb, "_ops_status_entries", lambda: [
        ("ok", "postgres", "pid 7"), ("down", "email", "token expired"),
        ("disabled", "whatsapp", "off")])

    lines = core.route("/status", USER).text.splitlines()

    assert lines[0] == "🔴 משהו לא עובד כרגע"
    assert lines[1] == "🟢 מסד הנתונים: תקין"  # no summary when all is well
    assert lines[2] == "🔴 מייל: לא עובד (⁨token expired⁩)"
    assert lines[3] == "⚪ וואטסאפ: כבוי"


def test_recon_changes_name_the_field_in_hebrew() -> None:
    child = MemoryItem(raw_text="x", extraction_meta={
        "recon_verdict": "update", "recon_fields": {"time": ["16:00", "17:30"]}})

    assert verdict_marker(child, lang="he") == "↩︎ מציע לעדכן: שעה: 17:30 (במקום 16:00)"
    assert verdict_marker(child, lang="en") == "↩︎ proposes: time: 16:00 → 17:30"


def test_bot_command_menu_in_hebrew() -> None:
    names = [name for name, _ in tb.bot_commands("he")]
    assert names == [name for name, _ in tb.BOT_COMMANDS]
    assert all(not _LATIN.search(desc) for _, desc in tb.bot_commands("he"))
    assert tb.bot_commands("en") == list(tb.BOT_COMMANDS)


def test_one_language_read_per_update(world, monkeypatch) -> None:
    core = _core()
    _seed_family(core, "he")
    card = core.route("פתק לבדיקה", USER)
    calls = []
    original = MemoryService.user_language

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(MemoryService, "user_language", counting)

    core.route("/review", USER)
    assert len(calls) == 1
    calls.clear()
    core.handle_callback(_find(card, "t"), USER)
    assert len(calls) == 1
    calls.clear()
    core._proposal_reply_for(_proposal_source())  # noqa: SLF001 - the card path
    assert len(calls) == 1


def test_hebrew_dates_carry_the_weekday(world) -> None:
    core = _core()
    _seed_family(core, "he")
    forwarded = datetime(2026, 11, 8, 7, 30, tzinfo=UTC)  # Sunday 09:30 in Israel

    card = core.route("הודעה מועברת", USER, forward={"sender": "שירה", "date": forwarded})
    recent = core.route("/recent", USER)

    assert "מתי: יום א׳ 08.11 09:30" in card.text
    assert "· יום א׳ 08.11" in recent.text
