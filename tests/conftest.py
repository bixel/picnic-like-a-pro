"""Shared pytest fixtures.

Fixtures
--------
fresh_picnic_client  (autouse)
    Replaces the Picnic client singleton with a fresh MockPicnicAPI for every
    test, so no test can reach the real Picnic API or trigger a real checkout.

isolated_db  (autouse)
    Points DB_PATH at a per-test temp SQLite file, resets the SQLAlchemy engine
    singletons so that path actually takes effect, and creates the schema.
    Being autouse means no test can ever touch the real data/picnic.db, even if
    it forgets to request a database fixture.

db_path
    The path to that isolated, empty database.

db_session
    An open AsyncSession against the empty database.

seeded_db_path
    Like db_path, but pre-populated with 25 orders spanning ~6 months.
    Use for tests that call MCP tools (which open their own sessions).

seeded_db_session
    An open AsyncSession against the seeded database, for direct query testing.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from picnic_meal_planner.db import engine as engine_mod
from picnic_meal_planner.picnic import client as client_mod
from picnic_meal_planner.picnic.mock_client import MockPicnicAPI, reset_mock_client


# ---------------------------------------------------------------------------
# Picnic API isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_picnic_client(monkeypatch):
    """Give every test an isolated, empty-cart mock Picnic API."""
    reset_mock_client()
    monkeypatch.setenv("PICNIC_MOCK", "true")
    mock = MockPicnicAPI()
    monkeypatch.setattr(client_mod, "_client", mock)
    return mock


# ---------------------------------------------------------------------------
# Database isolation
# ---------------------------------------------------------------------------

async def _reset_engine() -> None:
    """Dispose the cached engine and clear the module-level singletons.

    engine.py builds the engine once from DB_PATH and memoises it, so without
    this a second test would silently reuse the first test's engine (pointing
    at an already-deleted temp file).
    """
    if engine_mod._engine is not None:
        await engine_mod._engine.dispose()
    engine_mod._engine = None
    engine_mod._session_factory = None


@pytest_asyncio.fixture(autouse=True)
async def isolated_db(tmp_path, monkeypatch):
    """Give every test its own SQLite file, with the schema already created."""
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", path)

    await _reset_engine()
    await engine_mod.init_db()
    yield path
    await _reset_engine()


@pytest.fixture
def db_path(isolated_db):
    """Path to the isolated, empty test database."""
    return isolated_db


@pytest_asyncio.fixture
async def db_session(db_path):
    from picnic_meal_planner.db.queries import get_db

    async with get_db() as session:
        yield session


@pytest_asyncio.fixture
async def seeded_db_path(db_path):
    """Seed the isolated database with 6 months of history, return its path."""
    from picnic_meal_planner.db.queries import get_db
    from tests.fixtures.history import seed_database

    async with get_db() as session:
        await seed_database(session)
    return db_path


@pytest_asyncio.fixture
async def seeded_db_session(seeded_db_path):
    from picnic_meal_planner.db.queries import get_db

    async with get_db() as session:
        yield session
