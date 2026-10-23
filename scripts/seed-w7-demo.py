"""Seed brain_w7_demo with WhatsApp channels + PR B relations for screenshots.

Run: .venv/bin/python scripts/seed-w7-demo.py   (DATABASE_URL must be set)
Never pointed at the live DB by the caller (worker_7 only).
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from brain.db.models import ItemRelation, MemoryItem, WhatsAppPollState  # noqa: E402

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
now = datetime.now(timezone.utc)

A = uuid.uuid4()
B = uuid.uuid4()
C = uuid.uuid4()
F = uuid.uuid4()
T = uuid.uuid4()

FAMILY = "972501234567@s.whatsapp.net"
GROUP = "12345-67890@g.us"


def msg(id, sender, at_local, text, media=None):
    entry = {"id": id, "sender": sender, "at_local": at_local, "is_from_me": False}
    if media:
        entry["media"] = media
    entry["text"] = text
    return entry


with Session() as s:
    # --- relations demo (PR B views) --------------------------------------
    old = MemoryItem(
        id=A,
        raw_text="Call the bank about the card freeze",
        source="telegram",
        type="task",
        created_at=now - timedelta(days=2),
    )
    new = MemoryItem(
        id=B,
        raw_text="Call the bank tomorrow about the card freeze — line was busy today",
        source="whatsapp",
        type="task",
        created_at=now - timedelta(hours=3),
    )
    proposed_new = MemoryItem(
        id=C,
        raw_text="Bank visit moved to Thursday 10:00, bring the ID",
        source="whatsapp",
        type="task",
        created_at=now - timedelta(hours=1),
    )
    fact = MemoryItem(
        id=F,
        raw_text="The bank card was frozen on 20 Oct after three failed PIN tries",
        source="whatsapp",
        type="fact",
        created_at=now - timedelta(days=1),
    )
    task = MemoryItem(
        id=T,
        raw_text="Prepare the documents for the bank visit (ID + last statement)",
        source="whatsapp",
        type="task",
        created_at=now - timedelta(hours=1),
    )
    s.add_all([old, new, proposed_new, fact, task])
    s.flush()
    new.superseded_by = None
    old.superseded_by = B  # applied supersedes: strike
    s.add_all(
        [
            ItemRelation(
                from_id=B, to_id=A, kind="supersedes",
                provenance={"state": "applied", "how": "same_thread"},
            ),
            ItemRelation(
                from_id=C, to_id=A, kind="supersedes",
                provenance={"state": "proposed", "how": "llm_match"},
            ),
            ItemRelation(
                from_id=T, to_id=F, kind="derived_from",
                provenance={"state": "applied", "how": "llm_match"},
            ),
        ]
    )

    # --- whatsapp poll state (two channels) --------------------------------
    s.add_all(
        [
            WhatsAppPollState(
                chat_jid=FAMILY, channel_name="Family",
                cursor_utc=now - timedelta(minutes=4),
                last_poll_at=now - timedelta(minutes=1),
                last_message_at=now - timedelta(minutes=9),
                messages_seen=42, conversations_created=7, conversations_updated=3,
            ),
            WhatsAppPollState(
                chat_jid=GROUP, channel_name="Project group",
                cursor_utc=now - timedelta(hours=1, minutes=10),
                last_poll_at=now - timedelta(minutes=1),
                last_message_at=now - timedelta(days=2),
                messages_seen=15, conversations_created=2, conversations_updated=1,
            ),
        ]
    )

    # --- whatsapp conversation items ----------------------------------------
    fam_today = MemoryItem(
        raw_text=(
            "[WhatsApp · Family]\n"
            f"[{now.strftime('%d/%m/%Y, %H:%M')}] Ima: Don't forget the dentist tomorrow\n"
            f"[{now.strftime('%d/%m/%Y, %H:%M')}] Aba: photo of the parking ticket\n"
            "  image: ticket.jpg — attachment not read yet"
        ),
        source="whatsapp",
        type="note",
        meta={
            "kind": "whatsapp_conversation", "channel": "Family", "chat_jid": FAMILY,
            "first_message_at": (now - timedelta(minutes=30)).isoformat(),
            "last_message_at": (now - timedelta(minutes=5)).isoformat(),
            "messages": [
                msg("w1", "Ima", (now - timedelta(minutes=30)).isoformat(), "Don't forget the dentist tomorrow"),
                msg("m-photo-1", "Aba", (now - timedelta(minutes=20)).isoformat(), "",
                    {"media_type": "image", "filename": "ticket.jpg", "message_id": "m-photo-1",
                     "status": "saved", "attachment_item_id": str(F)}),
                msg("m-voice-1", "Ima", (now - timedelta(minutes=5)).isoformat(), "",
                    {"media_type": "audio", "filename": "voice.ogg", "message_id": "m-voice-1",
                     "status": "not read yet"}),
            ],
        },
        created_at=now - timedelta(hours=1),
    )
    fam_yesterday = MemoryItem(
        raw_text="[WhatsApp · Family]\n[22/10/2026, 21:14] Ima: Shabbat at ours, 19:00",
        source="whatsapp",
        type="note",
        created_at=now - timedelta(days=1),
        meta={
            "kind": "whatsapp_conversation", "channel": "Family", "chat_jid": FAMILY,
            "messages": [
                msg("y1", "Ima", (now - timedelta(days=1)).isoformat(), "Shabbat at ours, 19:00"),
            ],
        },
    )
    grp_today = MemoryItem(
        raw_text=(
            "[WhatsApp · Project group]\n"
            f"[{now.strftime('%d/%m/%Y, %H:%M')}] Dana: The staging deploy is green again\n"
            f"[{now.strftime('%d/%m/%Y, %H:%M')}] Ran: pdf of the incident report\n"
            "  document: incident-42.pdf — attachment not read yet"
        ),
        source="whatsapp",
        type="note",
        created_at=now - timedelta(hours=2),
        meta={
            "kind": "whatsapp_conversation", "channel": "Project group", "chat_jid": GROUP,
            "messages": [
                msg("g1", "Dana", (now - timedelta(hours=2)).isoformat(), "The staging deploy is green again"),
                msg("m-pdf-1", "Ran", (now - timedelta(hours=1)).isoformat(), "",
                    {"media_type": "document", "filename": "incident-42.pdf",
                     "message_id": "m-pdf-1", "status": "saved"}),
            ],
        },
    )
    s.add_all([fam_today, fam_yesterday, grp_today])
    s.commit()
    print("seeded:", {"family": str(FAMILY), "group": GROUP, "superseded_old": str(A), "current": str(B)})
