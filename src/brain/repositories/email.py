"""Data access for the email poller's seen-ledger (F2)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.db.models import EmailMessage


class EmailMessageRepository:
    """All data access for processed email messages; one instance per poll."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def seen(self, dedupe_id: str) -> bool:
        """True when a message with this dedupe id was already processed."""
        stmt = select(EmailMessage.id).where(EmailMessage.dedupe_id == dedupe_id)
        return self._db.execute(stmt).first() is not None

    def seen_gmail(self, gmail_id: str) -> bool:
        """True when this Gmail message id was already processed (fast path:
        lets the poller skip the full-message GET for known ids)."""
        stmt = select(EmailMessage.id).where(EmailMessage.gmail_id == gmail_id)
        return self._db.execute(stmt).first() is not None

    def mark(
        self,
        dedupe_id: str,
        gmail_id: str,
        rfc_message_id: str | None,
        item_id: uuid.UUID | None,
    ) -> None:
        """Record a processed message (idempotent by the unique dedupe_id)."""
        existing = self._db.execute(
            select(EmailMessage).where(EmailMessage.dedupe_id == dedupe_id)
        ).scalar_one_or_none()
        if existing is not None:
            return
        self._db.add(
            EmailMessage(
                dedupe_id=dedupe_id,
                gmail_id=gmail_id,
                rfc_message_id=rfc_message_id,
                item_id=item_id,
            )
        )
        self._db.commit()
