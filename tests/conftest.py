"""Shared pytest fixtures.

Fixtures
--------
fresh_picnic_client  (autouse)
    Replaces the Picnic client singleton with a fresh MockPicnicAPI for every
    test, so no test can reach the real Picnic API or trigger a real checkout.

db_path
    Points DB_PATH at an isolated temp SQLite file for the current test.

db_conn
    An open, initialised connection to that empty temp database.

seeded_db_path
    Like db_path, but pre-populated with 25 orders spanning ~6 months.
    Use for tests that call MCP tools (which open their own connections).

seeded_db_conn
    An open connection to the seeded database, for direct query testing.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from picnic_meal_planner.picnic import client as client_mod
from picnic_meal_planner.picnic.mock_client import MockPicnicAPI, reset_mock_client


@pytest.fixture(autouse=True)
def fresh_picnic_client(monkeypatch):
    """Give every test an isolated, empty-cart mock Picnic API."""
    reset_mock_client()
    monkeypatch.setenv("PICNIC_MOCK", "true")
    mock = MockPicnicAPI()
    monkeypatch.setattr(client_mod, "_client", mock)
    return mock


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Isolated SQLite database path, exported via the DB_PATH env var."""
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", path)
    return path


@pytest_asyncio.fixture
async def db_conn(db_path):
    from picnic_meal_planner.db.queries import get_db

    async with get_db() as conn:
        yield conn


@pytest_asyncio.fixture
async def seeded_db_path(db_path):
    """Seed the isolated database with 6 months of history, return its path."""
    from picnic_meal_planner.db.queries import get_db
    from tests.fixtures.history import seed_database

    async with get_db() as conn:
        await seed_database(conn)
    return db_path


@pytest_asyncio.fixture
async def seeded_db_conn(seeded_db_path):
    from picnic_meal_planner.db.queries import get_db

    async with get_db() as conn:
        yield conn
