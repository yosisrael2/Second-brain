"""F2: the email_messages seen-ledger on the real test database."""

import pytest
from sqlalchemy.exc import IntegrityError

from brain.db.models import EmailMessage
from brain.repositories.email import EmailMessageRepository


@pytest.fixture(name="email_db")
def email_db_fixture(database: None):
    from sqlalchemy import text

    from brain.db.session import SessionLocal, engine

    session = SessionLocal()
    yield session
    session.rollback()
    session.close()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM email_messages"))


@pytest.fixture(name="email_repo")
def email_repo_fixture(email_db):
    return EmailMessageRepository(email_db)


def test_seen_and_mark_roundtrip(email_repo):
    assert email_repo.seen("<m@x>") is False
    email_repo.mark("<m@x>", "gm1", "<m@x>", None)
    assert email_repo.seen("<m@x>") is True


def test_unique_index_backstops_the_ledger(email_db):
    """The DB constraint is the at-most-once guarantee's backstop."""
    email_db.add(EmailMessage(dedupe_id="<m@x>", gmail_id="gm1"))
    email_db.commit()
    email_db.add(EmailMessage(dedupe_id="<m@x>", gmail_id="gm2"))
    with pytest.raises(IntegrityError):
        email_db.commit()
    email_db.rollback()


def test_mark_is_idempotent(email_repo):
    email_repo.mark("<m@x>", "gm1", "<m@x>", None)
    email_repo.mark("<m@x>", "gm1", "<m@x>", None)
    assert email_repo.seen("<m@x>")


def test_item_id_set_null_on_item_delete(email_repo, email_db, clean_items):
    from brain.db.models import MemoryItem
    from brain.repositories.items import ItemsRepository

    repo = ItemsRepository(email_db)
    item = repo.add(MemoryItem(raw_text="email ledger link test", source="email"))
    email_repo.mark("<m@x>", "gm1", "<m@x>", item.id)
    row = email_db.query(EmailMessage).filter(EmailMessage.dedupe_id == "<m@x>").one()
    assert row.item_id == item.id
    repo.delete(item)
    repo.commit()
    email_db.expire_all()
    row = email_db.query(EmailMessage).filter(EmailMessage.dedupe_id == "<m@x>").one()
    assert row.item_id is None  # FK SET NULL; the ledger row stays: no double-capture
    assert row.gmail_id == "gm1"


def test_seen_gmail_fast_path(email_repo):
    assert email_repo.seen_gmail("gm1") is False
    email_repo.mark("<m@x>", "gm1", "<m@x>", None)
    assert email_repo.seen_gmail("gm1") is True
    assert email_repo.seen_gmail("gm2") is False
