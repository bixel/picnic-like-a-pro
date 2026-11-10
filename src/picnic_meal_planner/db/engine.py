"""SQLAlchemy async engine and session factory.

The module is lazy-initialised: no engine is created until the first call to
``get_engine()`` or ``get_db()``.  This avoids side-effects at import time and
makes it trivial to change ``DB_PATH`` via environment variables before any
database operation happens.

For local development the ``init_db()`` helper creates all tables via
``metadata.create_all``.  In production, run ``alembic upgrade head`` before
starting the application instead.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


def _db_url() -> str:
    db_path = os.getenv("DB_PATH", "data/picnic.db")
    return f"sqlite+aiosqlite:///{db_path}"


# The Alembic head this code expects; tests/test_migrations.py keeps it in step.
SCHEMA_REVISION = "0009"

# How long a write to the API request log waits for a database lock held by
# another connection before giving up.  sqlite3's default is 5 s, and the log
# is written inline on every Picnic/OpenRouter call: a lock held elsewhere (an
# import committing, a backup, the sqlite3 CLI) would otherwise stall each
# call by that much.  A lost log row is the cheaper failure.
LOG_LOCK_TIMEOUT_SECONDS = 0.2

# Module-level singletons; populated lazily.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_log_engine: AsyncEngine | None = None
_log_session_factory: async_sessionmaker[AsyncSession] | None = None


def _mute_driver_statement_logging() -> None:
    """Stop aiosqlite from logging every statement with its bound parameters.

    aiosqlite logs each operation at DEBUG including the parameter tuple, which
    for conversation_messages is the message content in plaintext. The app runs
    at INFO so this is normally invisible, but an operator enabling DEBUG to
    chase an unrelated bug should not silently start writing family
    conversations to stdout. Raise it back explicitly if you need driver-level
    tracing and understand what it emits.
    """
    logging.getLogger("aiosqlite").setLevel(logging.INFO)


def _create_engine(**connect_args) -> AsyncEngine:
    _mute_driver_statement_logging()
    # hide_parameters keeps bound values out of exception text. Without it,
    # SQLAlchemy appends "[parameters: ...]" to DBAPIError, so any logged
    # write failure would spill the row being written — for
    # conversation_messages that is the message content itself.
    engine = create_async_engine(
        _db_url(), echo=False, hide_parameters=True, connect_args=connect_args
    )

    # SQLite requires an explicit PRAGMA to honour FK constraints.
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _create_engine()
    return _engine


def get_log_engine() -> AsyncEngine:
    """A separate engine for the API request log, with a short lock timeout.

    Separate so the short timeout never applies to history or order writes,
    which should keep waiting the default 5 s rather than fail.
    """
    global _log_engine
    if _log_engine is None:
        _log_engine = _create_engine(timeout=LOG_LOCK_TIMEOUT_SECONDS)
    return _log_engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


def get_log_session_factory() -> async_sessionmaker[AsyncSession]:
    global _log_session_factory
    if _log_session_factory is None:
        _log_session_factory = async_sessionmaker(
            get_log_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _log_session_factory


async def dispose_engines() -> None:
    """Dispose both engines and forget them; the next use rebuilds from DB_PATH."""
    global _engine, _session_factory, _log_engine, _log_session_factory
    for engine in (_engine, _log_engine):
        if engine is not None:
            await engine.dispose()
    _engine = _session_factory = _log_engine = _log_session_factory = None


async def init_db() -> None:
    """Create all tables that do not yet exist.

    Suitable for development and fresh installs.  When Alembic is managing
    schema changes in production, run ``alembic upgrade head`` instead; that
    command is idempotent and safe to call on every deploy.
    """
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def pending_migration() -> str | None:
    """The stored Alembic revision if it is behind :data:`SCHEMA_REVISION`.

    Call it *before* :func:`init_db`.  On a database Alembic manages,
    ``create_all`` would otherwise create a table the next migration adds,
    and ``alembic upgrade head`` then fails with "table already exists".
    ``None`` when the database is current, ahead (a rolled-back image), or
    not under Alembic at all — :func:`missing_columns` covers that case.
    Revisions are zero-padded numbers, so they compare as strings.
    """

    def _read(sync_conn) -> str | None:
        if "alembic_version" not in inspect(sync_conn).get_table_names():
            return None
        row = sync_conn.execute(text("SELECT version_num FROM alembic_version")).first()
        return row[0] if row else ""

    async with get_engine().connect() as conn:
        stored = await conn.run_sync(_read)
    if stored is None or stored >= SCHEMA_REVISION:
        return None
    return stored or "(none)"


async def missing_columns() -> list[str]:
    """``table.column`` for every model column absent from the live database.

    ``create_all`` creates missing *tables* but never alters an existing one,
    so a database bootstrapped by :func:`init_db` and then upgraded without
    ``alembic upgrade head`` silently lacks newly added columns — and every
    ORM read of that table then fails. Tables that do not exist at all are
    skipped; ``create_all`` handles those.
    """

    def _diff(sync_conn) -> list[str]:
        inspector = inspect(sync_conn)
        existing = set(inspector.get_table_names())
        missing = []
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue
            live = {c["name"] for c in inspector.get_columns(table.name)}
            missing += [f"{table.name}.{c.name}" for c in table.columns if c.name not in live]
        return missing

    async with get_engine().connect() as conn:
        return await conn.run_sync(_diff)


@asynccontextmanager
async def get_db():
    """Async context manager that yields an open :class:`AsyncSession`."""
    async with get_session_factory()() as session:
        yield session


@asynccontextmanager
async def get_log_db():
    """Like :func:`get_db`, on the API request log's short-timeout engine."""
    async with get_log_session_factory()() as session:
        yield session
