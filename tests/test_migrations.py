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
