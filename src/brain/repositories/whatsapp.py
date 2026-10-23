"""Repository for WhatsApp poll state (one row per allowlisted chat)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.db.models import WhatsAppPollState


class WhatsAppStateRepository:
    """All data access for whatsapp_poll_state; same transaction discipline as
    :class:`~brain.repositories.items.ItemsRepository`: mutators flush, callers
    commit via the session (or let the poller commit).
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def get(self, chat_jid: str) -> WhatsAppPollState | None:
        return self._db.get(WhatsAppPollState, chat_jid)

    def all(self) -> list[WhatsAppPollState]:
        stmt = select(WhatsAppPollState).order_by(
            WhatsAppPollState.channel_name.asc(), WhatsAppPollState.chat_jid.asc()
        )
        return list(self._db.scalars(stmt))

    def get_or_create(self, chat_jid: str, *, channel_name: str | None = None) -> WhatsAppPollState:
        state = self.get(chat_jid)
        if state is None:
            state = WhatsAppPollState(chat_jid=chat_jid, channel_name=channel_name)
            self._db.add(state)
            self._db.flush()
        elif channel_name is not None and state.channel_name != channel_name:
            state.channel_name = channel_name
            self._db.flush()
        return state

    def record_poll(
        self,
        state: WhatsAppPollState,
        *,
        now: datetime,
        cursor_utc: datetime | None = None,
        history_until_utc: datetime | None = None,
        last_message_at: datetime | None = None,
        seen: int = 0,
        created: int = 0,
        updated: int = 0,
        open_item_id: UUID | None = None,
        set_open_item: bool = False,
    ) -> None:
        """Apply one poll's outcome to the state row (flush, no commit)."""
        state.last_poll_at = now
        if cursor_utc is not None:
            state.cursor_utc = cursor_utc
        if history_until_utc is not None:
            state.history_until_utc = history_until_utc
        if last_message_at is not None:
            state.last_message_at = last_message_at
        state.messages_seen += seen
        state.conversations_created += created
        state.conversations_updated += updated
        if set_open_item:
            state.open_item_id = open_item_id
        self._db.flush()
