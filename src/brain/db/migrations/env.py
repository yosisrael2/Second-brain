"""Alembic migration environment. Uses app settings for the DB URL.

LIVE GUARD: alembic builds its own engine here (engine_from_config below),
which BYPASSES the #45 engine-construction guard — a bare
``alembic upgrade`` from a worktree whose .env points at the live dbname
would migrate LIVE (incident 2026-10-24). env.py therefore re-runs the SAME
check (one definition of what live is: brain.config.assert_not_live_database)
before anything runs — online AND offline — refusing unless
BRAIN_ALLOW_LIVE=1 (the var the deploy/run-*.sh path already exports).
"""

from alembic import context
from sqlalchemy import engine_from_config, pool

from brain.config import assert_not_live_database, get_settings
from brain.db import models  # noqa: F401  (registers models on Base.metadata)
from brain.db.base import Base

config = context.config

# Single source of truth for the URL: app settings (env / .env).
_settings = get_settings()
config.set_main_option("sqlalchemy.url", _settings.database_url)

# Refuse the LIVE database before ANY migration runs (both modes). Same
# check, same message, same BRAIN_ALLOW_LIVE=1 opt-in as the engine factory.
assert_not_live_database(_settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout, no DB connection)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect and apply)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
