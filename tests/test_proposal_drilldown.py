"""The proposal card as a list + drill-down in ONE message.

One source yields N children; the card lists them (numbered) with one
button per undecided item. A tap opens that item's focus view in place
(Keep / Fix / Drop or its recon verdict buttons, then Back). After a
decision the list comes back on the page of the next undecided item; once
nothing is left the card collapses to a one-line summary.

Pure rendering tests use transient rows; the bot round trips run the REAL
TelegramBot over the *_test database. Invented names only (public repo).
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from brain.db.enums import ItemType
from brain.db.models import ItemRelation, ItemSource, MemoryItem
from brain.db.session import SessionLocal, engine
from brain.repositories.items import ItemsRepository
from brain.services.proposals import (
    CARD_TEXT_MAX,
    CB_PROPOSED_BACK,
    CB_PROPOSED_DROP,
    CB_PROPOSED_FIX,
    CB_PROPOSED_KEEP,
    CB_PROPOSED_KEEP_ALL,
    CB_PROPOSED_NOOP,
    CB_PROPOSED_OPEN,
    CB_PROPOSED_PAGE,
    focus_keyboard,
    item_button_label,
    page_count,
    page_of,
    proposal_keyboard,
    render_done,
    render_focus,
    render_source_card,
)
from conftest import assert_test_database

TZ = "Asia/Jerusalem"
USER = 7
BASE = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)


def _kids(n: int) -> list[MemoryItem]:
    """n transient proposed children, alternating kinds, every 3rd undated."""
    kinds = (ItemType.EVENT, ItemType.TASK, ItemType.REMINDER)
    rows = []
    for i in range(n):
        rows.append(
            MemoryItem(
                id=uuid.uuid4(),
                raw_text=f"Item {i + 1} for Romi's class",
                type=kinds[i % 3],
                occurred_at=None if i % 3 == 2 else BASE + timedelta(days=i),
                applies_to=["Romi"],
                child_state="proposed",
            )
        )
    return rows


def _source() -> MemoryItem:
    return MemoryItem(id=uuid.uuid4(), raw_text="Class update from Tamar", meta={})


def _datas(rows) -> list[str]:
    return [data for row in rows for _label, data in row]


def _labels(rows) -> list[list[str]]:
    return [[label for label, _data in row] for row in rows]


# --- the list keyboard ------------------------------------------------------------


class TestListKeyboard:
    def test_five_items_one_button_each_then_keep_all(self):
        kids = _kids(5)
        rows = proposal_keyboard("src", kids, TZ)
        assert len(rows) == 6  # 5 item rows + Keep all, no paging
        assert all(len(row) == 1 for row in rows)
        assert [row[0][1] for row in rows[:5]] == [f"{CB_PROPOSED_OPEN}:{k.id}" for k in kids]
        assert rows[-1] == [("✅ Keep all 5", f"{CB_PROPOSED_KEEP_ALL}:src")]

    def test_label_number_icon_title_and_date(self):
        kids = _kids(3)
        labels = [row[0][0] for row in proposal_keyboard("src", kids, TZ)[:3]]
        assert labels == [
            "1 · 📅 Item 1 for Romi's class · 02.11",
            "2 · ✔️ Item 2 for Romi's class · 03.11",
            "3 · 🔔 Item 3 for Romi's class",  # undated: no date part
        ]

    def test_long_titles_are_cut_at_24_chars(self):
        kid = MemoryItem(
            raw_text="A very long title about the end of year party", type=ItemType.TASK
        )
        assert item_button_label(kid, 4, TZ, flagged=False) == "4 · ✔️ A very long title about…"
        assert item_button_label(kid, 4, TZ, flagged=True).startswith("⚠️ 4 · ")

    def test_eight_items_still_fit_one_page(self):
        rows = proposal_keyboard("src", _kids(8), TZ)
        assert len(rows) == 9
        assert CB_PROPOSED_NOOP not in _datas(rows)

    def test_eleven_items_page_six_at_a_time(self):
        kids = _kids(11)
        first = proposal_keyboard("src", kids, TZ, page=0)
        second = proposal_keyboard("src", kids, TZ, page=1)
        assert len(first) == 6 + 2 and len(second) == 5 + 2
        assert _labels(first)[6] == ["◀", "1/2", "▶"]
        assert _datas([first[6]]) == [
            f"{CB_PROPOSED_PAGE}:src:1",  # ◀ wraps to the last page
            CB_PROPOSED_NOOP,
            f"{CB_PROPOSED_PAGE}:src:1",
        ]
        assert _labels(second)[5] == ["◀", "2/2", "▶"]
        assert first[0][0][0].startswith("1 · ") and second[0][0][0].startswith("7 · ")
        assert first[-1][0][0] == second[-1][0][0] == "✅ Keep all 11"

    def test_twenty_five_items_five_pages(self):
        kids = _kids(25)
        assert page_count(25) == 5
        last = proposal_keyboard("src", kids, TZ, page=4)
        assert len(last) == 1 + 2  # item 25, paging, Keep all
        assert last[0][0][0].startswith("25 · ")
        assert _labels(last)[1] == ["◀", "5/5", "▶"]

    def test_out_of_range_page_is_clamped(self):
        rows = proposal_keyboard("src", _kids(11), TZ, page=99)
        assert _labels(rows)[-2] == ["◀", "2/2", "▶"]

    def test_every_callback_fits_64_bytes_at_25_items(self):
        source_id = uuid.uuid4()
        kids = _kids(25)
        datas = []
        for page in range(page_count(25)):
            datas += _datas(proposal_keyboard(source_id, kids, TZ, page=page))
        for kid in kids:
            datas += _datas(focus_keyboard(source_id, kid, back_page=4))
        verdict = ("update", SimpleNamespace(id=uuid.uuid4(), provenance={}), None)
        datas += _datas(focus_keyboard(source_id, kids[0], recon_entry=verdict, back_page=4))
        assert datas and all(len(data.encode()) <= 64 for data in datas)

    def test_decided_items_lose_their_button_and_keep_all_counts_the_rest(self):
        kids = _kids(5)
        kids[0].child_state = "kept"
        kids[3].child_state = "dropped"
        rows = proposal_keyboard("src", kids, TZ)
        assert [row[0][0][:1] for row in rows[:3]] == ["2", "3", "5"]
        assert rows[-1][0][0] == "✅ Keep all 3"

    def test_page_of_follows_the_six_per_page_split(self):
        assert [page_of(i, 11) for i in (0, 5, 6, 10)] == [0, 0, 1, 1]
        assert page_of(7, 8) == 0  # no paging up to 8

    def test_hebrew_keep_all(self):
        rows = proposal_keyboard("src", _kids(2), TZ, lang="he")
        assert rows[-1][0][0] == "✅ שמור הכל (2)"


# --- list text, focus view, done summary ------------------------------------------


class TestRendering:
    def test_decided_lines_stay_with_a_prefix(self):
        kids = _kids(3)
        kids[0].child_state = "kept"
        kids[1].child_state = "dropped"
        kids[2].child_state = "kept"
        kids[2].extraction_meta = {"user_edited": True}
        lines = render_source_card(_source(), kids, TZ).split("\n")
        assert lines[1].startswith("✅ 1. 📅 Item 1")
        assert lines[2].startswith("🗑 <s>2. ✔️ Item 2") and lines[2].endswith("</s>")
        assert lines[3].startswith("✏️ 3. 🔔 Item 3")

    def test_focus_view_text(self):
        kids = _kids(3)
        text_ = render_focus(_source(), kids, 1, TZ, quote="Please send <the> form by Tuesday")
        assert text_.split("\n") == [
            "🧠 Found 3 items in: Class update from Tamar",
            "<b>2 / 3 · ✔️ Item 2 for Romi's class</b>",
            "Tue 03.11 09:00 · Romi",
            "<i>Please send &lt;the&gt; form by Tuesday</i>",
        ]

    def test_focus_keyboard_keep_fix_drop_then_back(self):
        kid = _kids(1)[0]
        rows = focus_keyboard("src", kid, back_page=2)
        assert _labels(rows) == [["✅ Keep", "✏️ Fix", "🗑 Drop"], ["↩︎ Back to list"]]
        assert _datas(rows) == [
            f"{CB_PROPOSED_KEEP}:{kid.id}",
            f"{CB_PROPOSED_FIX}:{kid.id}",
            f"{CB_PROPOSED_DROP}:{kid.id}",
            f"{CB_PROPOSED_BACK}:src:2",
        ]
        assert focus_keyboard("src", kid, back_page=None) == rows[:1]  # one-item card
        assert _labels(focus_keyboard("src", kid, lang="he"))[1] == ["↩︎ חזרה לרשימה"]

    def test_verdict_item_focus_shows_its_verdict_buttons(self):
        kid = _kids(1)[0]
        rel = SimpleNamespace(id=uuid.uuid4(), provenance={"state": "proposed"})
        rows = focus_keyboard("src", kid, recon_entry=("cancellation", rel, None))
        assert _labels(rows) == [
            ["✅ Confirm cancellation", "✖ Not cancelled"],
            ["↩︎ Back to list"],
        ]
        assert _datas(rows)[:2] == [f"rc:{rel.id}", f"rd:{rel.id}"]
        detail = focus_keyboard("src", kid, recon_entry=("more_detail", rel, None))
        assert _labels(detail)[0] == ["✅ Keep", "✏️ Fix", "🗑 Drop"]

    def test_done_summary_collapses_the_list(self):
        kids = _kids(4)
        for kid, state in zip(kids, ("kept", "kept", "dropped", "kept"), strict=True):
            kid.child_state = state
        kids[3].extraction_meta = {"user_edited": True}
        done = render_done(_source(), kids, TZ)
        first, rest = done.split("\n", 1)
        assert first == "✅ 2 kept · ✏️ 1 fixed · 🗑 1 dropped"
        assert rest.startswith("<blockquote expandable>🧠 Found 4 items")
        assert rest.endswith("</blockquote>")
        assert rest.count("<blockquote") == 1  # never nested

    def test_done_summary_omits_zero_parts_in_hebrew(self):
        kids = _kids(2)
        for kid in kids:
            kid.child_state = "kept"
        assert render_done(_source(), kids, TZ, lang="he").split("\n")[0] == "✅ 2 נשמרו"


# --- bot round trips over the test database ---------------------------------------


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


def _bot(world):
    from brain.services.items import ItemsService
    from brain.telegram_bot import BotServices, TelegramBot

    repo, _session = world
    items = ItemsService(repo)

    @contextmanager
    def factory():
        yield BotServices(ingest=None, items=items, search=None, ask=None)

    return TelegramBot(
        services_factory=factory, allowed_ids=frozenset({USER}), timezone=ZoneInfo(TZ)
    )


def _seed(world, n: int, *, fyi: int = 0) -> tuple[MemoryItem, list[MemoryItem]]:
    """A source with n actionable children (+ fyi 'not for us' facts), in a
    fixed card order (created_at one second apart)."""
    repo, session = world
    source = repo.add(MemoryItem(raw_text="Class update from Tamar"))
    repo.commit()
    children = []
    for i in range(n + fyi):
        is_fyi = i >= n
        child = repo.add(
            MemoryItem(
                raw_text=f"Note {i + 1} about the play" if is_fyi else f"Task {i + 1} for Ilan",
                type=ItemType.FACT if is_fyi else ItemType.TASK,
                applies_to=["nobody"] if is_fyi else ["Ilan"],
                child_state="proposed",
                created_at=BASE + timedelta(seconds=i),
            )
        )
        session.flush()
        session.add(ItemSource(child_id=child.id, source_id=source.id, quote=f"quote {i + 1}"))
        children.append(child)
    repo.commit()
    return source, children


def _find(reply, prefix: str, nth: int = 0) -> str:
    found = [d for row in reply.keyboard or [] for _l, d in row if d.split(":")[0] == prefix]
    assert len(found) > nth, f"no {prefix} #{nth} in {reply.keyboard}"
    return found[nth]


def _states(session, children) -> list[str | None]:
    session.expire_all()
    return [session.get(MemoryItem, c.id).child_state for c in children]


class TestBotDrillDown:
    def test_one_actionable_item_opens_in_focus(self, world):
        source, _children = _seed(world, 1, fyi=2)
        card = _bot(world)._proposal_reply_for(source)
        assert card.text.split("\n")[1].startswith("<b>1 / 1 · ✔️ Task 1 for Ilan</b>")
        assert "<blockquote expandable>" in card.text  # the FYI lines stay readable
        assert _labels(card.keyboard) == [["✅ Keep", "✏️ Fix", "🗑 Drop"]]  # no Back/Keep all

    def test_single_item_keep_collapses_to_done(self, world):
        source, children = _seed(world, 1)
        bot = _bot(world)
        card = bot._proposal_reply_for(source)
        done = bot.handle_callback(_find(card, CB_PROPOSED_KEEP), USER)
        assert done.text.startswith("✅ 1 kept\n<blockquote expandable>")
        assert done.keyboard is None and done.edit is True
        assert done.toast == "✅ Kept: Task 1 for Ilan"

    def test_open_back_and_page_round_trip(self, world):
        source, children = _seed(world, 11)
        bot = _bot(world)
        card = bot._proposal_reply_for(source)
        assert card.edit is False  # the first send is the only new message
        page2 = bot.handle_callback(f"{CB_PROPOSED_PAGE}:{source.id}:1", USER)
        assert page2.edit is True and page2.text == card.text
        assert page2.keyboard[0][0][0].startswith("7 · ")
        focus = bot.handle_callback(_find(page2, CB_PROPOSED_OPEN, 2), USER)  # item 9
        assert focus.edit is True
        assert focus.text.split("\n")[1] == "<b>9 / 11 · ✔️ Task 9 for Ilan</b>"
        assert "<i>quote 9</i>" in focus.text
        back = _find(focus, CB_PROPOSED_BACK)
        assert back == f"{CB_PROPOSED_BACK}:{source.id}:1"  # back to the page it came from
        listed = bot.handle_callback(back, USER)
        assert listed.keyboard == page2.keyboard
        bare = bot.handle_callback(f"{CB_PROPOSED_BACK}:{source.id}", USER)
        assert bare.keyboard == card.keyboard  # pb without a page: the first page
        noop = bot.handle_callback(CB_PROPOSED_NOOP, USER)
        assert noop.text == "" and noop.toast is None  # a plain ack, nothing changes
        assert _states(world[1], children) == ["proposed"] * 11

    def test_keep_from_focus_returns_to_the_page_of_the_next_item(self, world):
        source, children = _seed(world, 11)
        bot = _bot(world)
        focus = bot.handle_callback(f"{CB_PROPOSED_OPEN}:{children[5].id}", USER)  # item 6
        kept = bot.handle_callback(_find(focus, CB_PROPOSED_KEEP), USER)
        assert kept.toast == "✅ Kept: Task 6 for Ilan"
        # next undecided = item 7: 10 left, item 7 is the 6th → still page 1
        assert kept.keyboard[0][0][0].startswith("1 · ")
        assert "✅ 6. ✔️ Task 6 for Ilan" in kept.text
        assert kept.keyboard[-1][0][0] == "✅ Keep all 10"
        focus = bot.handle_callback(f"{CB_PROPOSED_OPEN}:{children[7].id}", USER)  # item 8
        dropped = bot.handle_callback(_find(focus, CB_PROPOSED_DROP), USER)
        # next = item 9, the 7th of the 9 left → page 2 (items 9, 10, 11)
        assert [row[0][0][:2] for row in dropped.keyboard[:3]] == ["9 ", "10", "11"]
        assert _labels(dropped.keyboard)[-2] == ["◀", "2/2", "▶"]
        assert "🗑 Dropped" in dropped.toast
        assert _states(world[1], children)[5:8] == ["kept", "proposed", "dropped"]

    def test_deciding_the_last_item_wraps_to_the_first_undecided(self, world):
        source, children = _seed(world, 3)
        bot = _bot(world)
        focus = bot.handle_callback(f"{CB_PROPOSED_OPEN}:{children[2].id}", USER)
        reply = bot.handle_callback(_find(focus, CB_PROPOSED_KEEP), USER)
        assert [row[0][0][:1] for row in reply.keyboard[:-1]] == ["1", "2"]

    def test_everything_decided_collapses_to_the_summary(self, world):
        source, children = _seed(world, 3, fyi=1)
        bot = _bot(world)
        for data in (
            f"{CB_PROPOSED_KEEP}:{children[0].id}",
            f"{CB_PROPOSED_DROP}:{children[1].id}",
        ):
            bot.handle_callback(data, USER)
        last = bot.handle_callback(f"{CB_PROPOSED_KEEP}:{children[2].id}", USER)
        assert last.text.split("\n")[0] == "✅ 2 kept · 🗑 1 dropped"
        assert last.keyboard is None
        assert "ℹ️ Note 4 about the play" in last.text  # FYI lines inside the quote

    def test_old_card_pk_button_still_acts_and_rerenders_new_style(self, world):
        source, children = _seed(world, 3)
        bot = _bot(world)
        # a card sent before the redesign carried pk/px/pf:<child-uuid> per row
        reply = bot.handle_callback(f"{CB_PROPOSED_KEEP}:{children[1].id}", USER)
        assert reply.edit is True and "✅ Kept" in reply.toast
        assert all(d.split(":")[0] in ("po", "pa") for d in _datas(reply.keyboard))
        assert _states(world[1], children) == ["proposed", "kept", "proposed"]

    def test_keep_all_counts_and_keeps_the_rest(self, world):
        source, children = _seed(world, 4, fyi=1)
        bot = _bot(world)
        bot.handle_callback(f"{CB_PROPOSED_DROP}:{children[0].id}", USER)
        card = bot.handle_callback(f"{CB_PROPOSED_PAGE}:{source.id}:0", USER)
        assert card.keyboard[-1][0] == ("✅ Keep all 3", f"{CB_PROPOSED_KEEP_ALL}:{source.id}")
        done = bot.handle_callback(_find(card, CB_PROPOSED_KEEP_ALL), USER)
        assert done.toast == "✅ Kept 3 item(s)."  # the count on the button
        assert done.text.split("\n")[0] == "✅ 3 kept · 🗑 1 dropped"
        assert _states(world[1], children) == ["dropped", "kept", "kept", "kept", "kept"]

    def test_an_fyi_only_card_offers_keep_all_then_closes(self, world):
        source, children = _seed(world, 0, fyi=2)
        bot = _bot(world)
        card = bot._proposal_reply_for(source)
        assert _labels(card.keyboard) == [["✅ Keep all 2"]]
        done = bot.handle_callback(_find(card, CB_PROPOSED_KEEP_ALL), USER)
        assert done.keyboard is None and done.edit is True
        assert done.text.startswith("✓ all done here.\n<blockquote expandable>")
        assert _states(world[1], children) == ["kept", "kept"]

    def test_stale_taps_toast_and_rerender(self, world):
        source, children = _seed(world, 3)
        bot = _bot(world)
        focus = bot.handle_callback(f"{CB_PROPOSED_OPEN}:{children[0].id}", USER)
        bot.handle_callback(_find(focus, CB_PROPOSED_KEEP), USER)
        for data in (f"{CB_PROPOSED_OPEN}:{children[0].id}", _find(focus, CB_PROPOSED_DROP)):
            stale = bot.handle_callback(data, USER)
            assert "already handled" in stale.toast
            assert stale.edit is True and stale.text.startswith("🧠 Found 3 items")
            assert _datas(stale.keyboard)[0] == f"{CB_PROPOSED_OPEN}:{children[1].id}"
        assert _states(world[1], children) == ["kept", "proposed", "proposed"]

    def test_malformed_navigation_changes_nothing(self, world):
        source, children = _seed(world, 2)
        bot = _bot(world)
        cases = [
            f"{CB_PROPOSED_OPEN}:not-a-uuid",
            f"{CB_PROPOSED_OPEN}:{uuid.uuid4()}",
            f"{CB_PROPOSED_OPEN}:{children[0].id}:x",
            f"{CB_PROPOSED_OPEN}:{source.id}",  # a source is no child
            f"{CB_PROPOSED_PAGE}:{source.id}",  # pg needs a page
            f"{CB_PROPOSED_PAGE}:{source.id}:-1",
            f"{CB_PROPOSED_PAGE}:{source.id}:99999",
            f"{CB_PROPOSED_PAGE}:{uuid.uuid4()}:0",
            f"{CB_PROPOSED_BACK}:junk",
            f"{CB_PROPOSED_BACK}:{children[0].id}:0",  # a child id is no source
        ]
        for data in cases:
            reply = bot.handle_callback(data, USER)
            assert reply.text == "" and reply.keyboard is None, data
            assert reply.toast.startswith("⌛"), (data, reply.toast)
        assert _states(world[1], children) == ["proposed", "proposed"]

    def test_verdict_item_in_a_list_opens_its_verdict_buttons(self, world):
        repo, session = world
        source, children = _seed(world, 1)
        old = repo.add(MemoryItem(raw_text="Ilan's swim class on Sunday 16:00"))
        child = repo.add(
            MemoryItem(
                raw_text="Ilan's swim class on Sunday 17:30",
                child_state="proposed",
                created_at=BASE + timedelta(seconds=10),
                extraction_meta={
                    "recon_verdict": "conflict",
                    "recon_fields": {"time": ["16:00", "17:30"]},
                },
            )
        )
        session.flush()
        session.add(ItemSource(child_id=child.id, source_id=source.id, quote="swim 17:30"))
        session.add(
            ItemRelation(
                from_id=child.id, to_id=old.id, kind="conflicts", provenance={"how": "recon"}
            )
        )
        repo.commit()
        bot = _bot(world)
        card = bot._proposal_reply_for(source)
        assert card.keyboard[1][0][0].startswith("⚠️ 2 · ")
        focus = bot.handle_callback(card.keyboard[1][0][1], USER)
        assert _labels(focus.keyboard) == [
            ["✅ Keep existing", "✅ Keep new"],
            ["↩︎ Back to list"],
        ]
        assert "which is right?" in focus.text  # the both-versions block
        reply = bot.handle_callback(_find(focus, "rd"), USER)
        assert "✅" in reply.toast
        assert reply.keyboard[0][0][1] == f"{CB_PROPOSED_OPEN}:{children[0].id}"


class TestFixFlowCard:
    def test_fix_prompt_names_the_item_and_the_card_rerenders(self, world):
        source, children = _seed(world, 3)
        bot = _bot(world)
        focus = bot.handle_callback(f"{CB_PROPOSED_OPEN}:{children[1].id}", USER)
        prompt = bot.handle_callback(
            _find(focus, CB_PROPOSED_FIX), USER, chat_id=555, message_id=42
        )
        assert prompt.text.startswith("✏️ New title for #2 Task 2 for Ilan")
        assert prompt.silent is True and prompt.edit is False
        bot.route("Task 2 for Ilan, signed", USER)
        done = bot.route("keep", USER)
        assert done.text.startswith("✏️ Fixed: Task 2 for Ilan, signed")
        [(chat_id, message_id, card)] = done.followups
        assert (chat_id, message_id) == (555, 42)
        assert card.edit is True
        assert "✏️ 2. ✔️ Task 2 for Ilan, signed" in card.text
        assert [row[0][0][:1] for row in card.keyboard[:-1]] == ["1", "3"]

    def test_fix_without_the_card_origin_has_no_followup(self, world):
        source, children = _seed(world, 2)
        bot = _bot(world)
        bot.handle_callback(f"{CB_PROPOSED_FIX}:{children[0].id}", USER)
        bot.route("New name", USER)
        assert bot.route("keep", USER).followups == ()

    def test_hebrew_fix_prompt(self, world):
        source, children = _seed(world, 2)
        bot = _bot(world)
        bot._read_language = lambda: "he"  # noqa: SLF001 — the stored language
        prompt = bot.handle_callback(f"{CB_PROPOSED_FIX}:{children[1].id}", USER)
        assert prompt.text.startswith("✏️ מתקנים את #2 ")


# --- the adapter --------------------------------------------------------------------


class _TG:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.sent.append(kwargs)

    async def edit_message_text(self, **kwargs) -> None:
        self.edits.append(kwargs)


class TestAdapter:
    def test_callback_passes_the_card_origin(self):
        from brain.telegram_bot import Reply, _handle_update

        seen = {}

        class Core:
            def is_allowed(self, user_id) -> bool:
                return True

            def handle_callback(self, data, user_id=None, **origin) -> Reply:
                seen.update(origin)
                return Reply("🧠 card", edit=True)

        async def answer(**_kwargs) -> None:
            return None

        query = SimpleNamespace(
            data="po:x", message=SimpleNamespace(chat_id=555, message_id=9), answer=answer
        )
        tg = _TG()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), callback_query=query)
        asyncio.run(_handle_update(tg, Core(), update))
        assert seen == {"chat_id": 555, "message_id": 9}
        assert [e["message_id"] for e in tg.edits] == [9] and tg.sent == []

    def test_text_reply_followups_edit_the_card(self):
        from brain.telegram_bot import Reply, _handle_update

        card = Reply("🧠 list", keyboard=[[("1 · ✔️ Task", "po:x")]], edit=True)

        class Core:
            def is_allowed(self, user_id) -> bool:
                return True

            def is_question(self, text_: str) -> bool:
                return False

            def route(self, text_, user_id, forward=None) -> Reply:
                return Reply("✏️ Fixed", followups=((555, 42, card),))

        message = SimpleNamespace(
            chat_id=555,
            text="keep",
            document=None,
            photo=None,
            voice=None,
            video_note=None,
            forward_origin=None,
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1), callback_query=None, effective_message=message
        )
        tg = _TG()
        asyncio.run(_handle_update(tg, Core(), update))
        assert [m["text"] for m in tg.sent] == ["✏️ Fixed"]
        assert [(e["message_id"], e["text"]) for e in tg.edits] == [(42, "🧠 list")]

    def test_an_unchanged_card_edit_never_posts_a_new_card(self):
        """A double tap (same pg:, a stale pk on an up-to-date card) renders
        the same text + keyboard; Telegram answers 'Message is not modified'.
        That edit is done — it must not fall back to a brand-new card."""
        from brain.telegram_bot import Reply, _send

        class BadRequest(Exception):  # PTB's class name is what is matched
            pass

        class TG(_TG):
            async def edit_message_text(self, **kwargs) -> None:
                self.edits.append(kwargs)
                raise BadRequest(
                    "Message is not modified: specified new message content and reply "
                    "markup are exactly the same as a current content and reply markup"
                )

        tg = TG()
        card = Reply("🧠 list", keyboard=[[("1 · ✔️ Task", "po:x")]], edit=True)
        asyncio.run(_send(tg, 555, card, message_id=9))
        assert len(tg.edits) == 1 and tg.sent == []

    def test_other_edit_failures_still_fall_back_to_a_new_message(self):
        from brain.telegram_bot import Reply, _send

        class BadRequest(Exception):
            pass

        class TG(_TG):
            async def edit_message_text(self, **kwargs) -> None:
                raise BadRequest("Message to edit not found")

        tg = TG()
        asyncio.run(_send(tg, 555, Reply("🧠 list", edit=True), message_id=9))
        assert [m["text"] for m in tg.sent] == ["🧠 list"]


class TestLongCards:
    """The adapter hard-cuts a reply at 4000 characters, which could land
    mid-tag; a card drops whole trailing lines instead ("… +k more")."""

    @staticmethod
    def _long_kids(n: int, fyi: int = 0) -> list[MemoryItem]:
        kids = _kids(n + fyi)
        for i, kid in enumerate(kids):
            kid.raw_text = f"Item {i + 1}: " + "bring the signed form for Romi & Ilan " * 3
            if i >= n:
                kid.type, kid.occurred_at, kid.applies_to = ItemType.FACT, None, ["nobody"]
        return kids

    def test_long_list_stays_under_the_limit_with_whole_lines(self):
        card = render_source_card(_source(), self._long_kids(60, fyi=30), TZ)
        assert len(card) <= CARD_TEXT_MAX
        lines = card.split("\n")
        # one marker for the numbered lines, one inside the FYI blockquote
        assert sum(1 for line in lines if line.startswith("… +")) == 2
        assert " more</blockquote>" in card  # the quote still closes
        assert card.count("<blockquote expandable>") == card.count("</blockquote>") == 1
        assert card.endswith("</blockquote>")

    def test_long_done_summary_closes_its_blockquote(self):
        kids = self._long_kids(60)
        for kid in kids:
            kid.child_state = "kept"
        done = render_done(_source(), kids, TZ, lang="he")
        assert len(done) <= CARD_TEXT_MAX
        assert done.split("\n")[0] == "✅ 60 נשמרו"
        assert done.endswith("</blockquote>") and done.count("<blockquote") == 1
        assert "… ועוד " in done

    def test_short_cards_are_untouched(self):
        assert "more" not in render_source_card(_source(), _kids(5), TZ)
