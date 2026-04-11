"""Alembic migration environment — async SQLite via aiosqlite.

Key points:
- The database URL is derived from the ``DB_PATH`` env var (same as the app).
- ``render_as_batch=True`` is required for SQLite, which does not support
  most ALTER TABLE operations natively.  Alembic implements them via table
  rebuild in batch mode.
- The ORM ``Base.metadata`` is passed to Alembic so that ``alembic revision
  --autogenerate`` can diff the current schema against the models.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Make sure the src package is importable when Alembic is run from the
# project root (e.g. ``uv run alembic upgrade head``).
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from picnic_meal_planner.db.models import Base  # noqa: E402

# ---------------------------------------------------------------------------
# Alembic Config object (provides .ini file values)
# ---------------------------------------------------------------------------

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_url() -> str:
    db_path = os.getenv("DB_PATH", "data/picnic.db")
    return f"sqlite+aiosqlite:///{db_path}"


# ---------------------------------------------------------------------------
# Offline mode (no live DB connection; generates raw SQL)
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode (connects to the DB and runs migrations)
# ---------------------------------------------------------------------------

def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _get_url()

    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
