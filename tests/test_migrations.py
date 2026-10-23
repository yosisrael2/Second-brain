"""Alembic migration hygiene: exactly one head on the versions directory.

Two workers add migrations in parallel (cockpit ops tables, capture fields);
whichever PR merges second must re-point its ``down_revision`` to the new
head. This test fails the PR that would create a second head on main.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_migrations_have_exactly_one_head():
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "src/brain/db/migrations"))
    script = ScriptDirectory.from_config(cfg)

    heads = script.get_heads()

    assert len(heads) == 1, (
        f"multiple alembic heads {heads}: re-point the newer migration's "
        "down_revision at the other head so main has a single linear history"
    )


def test_item_relations_round_trips_on_a_fresh_database(tmp_path):
    """PR B: b8c4f2a6e9d1 upgrades cleanly and downgrades without residue.

    Runs the REAL alembic up -> down -> up against a throwaway database so
    the item_relations table + memory_items.superseded_by round-trip is
    proven, not assumed (same standard as the attachments migration).
    """
    import sqlalchemy as sa
    from alembic import command

    from brain.db.session import engine

    url = engine.url.render_as_string(hide_password=False)
    assert url.rsplit("/", 1)[-1].endswith("_test"), f"not a test db: {url}"
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("DROP TABLE IF EXISTS item_relations CASCADE"))
            conn.execute(sa.text("DROP TABLE IF EXISTS attachments CASCADE"))
            conn.execute(sa.text("DELETE FROM alembic_version"))
        command.upgrade(_cfg(), "heads")
        with engine.connect() as conn:
            cols = {
                row[0]
                for row in conn.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name='item_relations'"
                    )
                )
            }
            assert {"id", "from_id", "to_id", "kind", "provenance", "created_at"} <= cols
            superseded = conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name='memory_items' AND column_name='superseded_by'"
                )
            ).scalar_one_or_none()
            assert superseded == "superseded_by"
        command.downgrade(_cfg(), "a5c8f1d9e3b7")
        with engine.connect() as conn:
            gone = conn.execute(
                sa.text("SELECT to_regclass('item_relations')")
            ).scalar_one_or_none()
            assert gone is None, "downgrade must drop item_relations"
            leftover = conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name='memory_items' AND column_name='superseded_by'"
                )
            ).scalar_one_or_none()
            assert leftover is None, "downgrade must drop superseded_by"
        command.upgrade(_cfg(), "heads")  # leave the suite db at the new head
    finally:
        engine.dispose()


def _cfg():
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "src/brain/db/migrations"))
    return cfg
