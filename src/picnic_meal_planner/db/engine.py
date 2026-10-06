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

from sqlalchemy import event
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


# Module-level singletons; populated lazily.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


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


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _mute_driver_statement_logging()
        # hide_parameters keeps bound values out of exception text. Without it,
        # SQLAlchemy appends "[parameters: ...]" to DBAPIError, so any logged
        # write failure would spill the row being written — for
        # conversation_messages that is the message content itself.
        _engine = create_async_engine(
            _db_url(), echo=False, hide_parameters=True
        )

        # SQLite requires an explicit PRAGMA to honour FK constraints.
        @event.listens_for(_engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def init_db() -> None:
    """Create all tables that do not yet exist.

    Suitable for development and fresh installs.  When Alembic is managing
    schema changes in production, run ``alembic upgrade head`` instead; that
    command is idempotent and safe to call on every deploy.
    """
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_db():
    """Async context manager that yields an open :class:`AsyncSession`."""
    async with get_session_factory()() as session:
        yield session
