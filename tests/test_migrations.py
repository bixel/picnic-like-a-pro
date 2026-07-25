"""Alembic migration tests.

The project has two sources of schema truth:

  * ``Base.metadata.create_all()`` — used by ``init_db()``, local development,
    and the whole test suite.
  * ``alembic upgrade head``      — used in production deployments.

Nothing keeps those in step automatically, so a model change that lands without
a matching migration would leave the test suite green while production runs a
different schema.  The tests here compare the two schemas structurally and fail
on any drift.

These tests are synchronous on purpose: ``alembic/env.py`` calls
``asyncio.run()`` in online mode, which cannot be nested inside a running loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from picnic_meal_planner.db.models import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


def _schema_snapshot(db_file: Path) -> dict:
    """Reflect *db_file* into a comparable dict of its structure."""
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        inspector = inspect(engine)
        snapshot: dict = {}
        for table in sorted(inspector.get_table_names()):
            if table == "alembic_version":  # bookkeeping, not part of the model
                continue
            snapshot[table] = {
                "columns": {
                    col["name"]: {
                        "type": str(col["type"]),
                        "nullable": col["nullable"],
                    }
                    for col in inspector.get_columns(table)
                },
                "primary_key": sorted(
                    inspector.get_pk_constraint(table)["constrained_columns"]
                ),
                "foreign_keys": sorted(
                    (
                        tuple(fk["constrained_columns"]),
                        fk["referred_table"],
                        tuple(fk["referred_columns"]),
                    )
                    for fk in inspector.get_foreign_keys(table)
                ),
                "unique_constraints": sorted(
                    tuple(uc["column_names"])
                    for uc in inspector.get_unique_constraints(table)
                ),
            }
        return snapshot
    finally:
        engine.dispose()


@pytest.fixture
def migrated_db(tmp_path, monkeypatch) -> Path:
    """A database built by running ``alembic upgrade head``."""
    db_file = tmp_path / "migrated.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    command.upgrade(_alembic_config(), "head")
    return db_file


@pytest.fixture
def orm_db(tmp_path) -> Path:
    """A database built by ``Base.metadata.create_all()``."""
    db_file = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return db_file


# ---------------------------------------------------------------------------
# Migration runs at all
# ---------------------------------------------------------------------------

class TestMigrationRuns:
    def test_upgrade_head_creates_database(self, migrated_db):
        assert migrated_db.exists()

    def test_upgrade_head_creates_all_four_tables(self, migrated_db):
        tables = set(_schema_snapshot(migrated_db))
        assert tables == {"products", "orders", "order_items", "import_checkpoints"}

    def test_upgrade_stamps_alembic_version(self, migrated_db):
        engine = create_engine(f"sqlite:///{migrated_db}")
        try:
            assert "alembic_version" in inspect(engine).get_table_names()
        finally:
            engine.dispose()

    def test_downgrade_removes_all_tables(self, migrated_db):
        command.downgrade(_alembic_config(), "base")
        assert set(_schema_snapshot(migrated_db)) == set()

    def test_upgrade_is_reapplicable_after_downgrade(self, migrated_db):
        before = _schema_snapshot(migrated_db)
        command.downgrade(_alembic_config(), "base")
        command.upgrade(_alembic_config(), "head")
        assert _schema_snapshot(migrated_db) == before


# ---------------------------------------------------------------------------
# Migration vs. ORM models — drift detection
# ---------------------------------------------------------------------------

class TestMigrationMatchesModels:
    def test_same_tables(self, migrated_db, orm_db):
        assert set(_schema_snapshot(migrated_db)) == set(_schema_snapshot(orm_db))

    @pytest.mark.parametrize(
        "table", ["products", "orders", "order_items", "import_checkpoints"]
    )
    def test_same_columns(self, migrated_db, orm_db, table):
        migrated = _schema_snapshot(migrated_db)[table]["columns"]
        orm = _schema_snapshot(orm_db)[table]["columns"]
        assert migrated == orm, (
            f"Schema drift in {table!r}: the Alembic migration and the ORM "
            f"models disagree. Add a migration for the model change."
        )

    @pytest.mark.parametrize(
        "table", ["products", "orders", "order_items", "import_checkpoints"]
    )
    def test_same_primary_keys(self, migrated_db, orm_db, table):
        assert (
            _schema_snapshot(migrated_db)[table]["primary_key"]
            == _schema_snapshot(orm_db)[table]["primary_key"]
        )

    def test_same_foreign_keys(self, migrated_db, orm_db):
        migrated = _schema_snapshot(migrated_db)["order_items"]["foreign_keys"]
        orm = _schema_snapshot(orm_db)["order_items"]["foreign_keys"]
        assert migrated == orm

    def test_same_unique_constraints(self, migrated_db, orm_db):
        migrated = _schema_snapshot(migrated_db)["orders"]["unique_constraints"]
        orm = _schema_snapshot(orm_db)["orders"]["unique_constraints"]
        assert migrated == orm

    def test_full_schema_snapshots_are_identical(self, migrated_db, orm_db):
        assert _schema_snapshot(migrated_db) == _schema_snapshot(orm_db)


# ---------------------------------------------------------------------------
# The migrated schema actually works with the app's queries
# ---------------------------------------------------------------------------

class TestMigratedSchemaIsUsable:
    def test_seeded_history_round_trips_on_migrated_schema(self, migrated_db):
        """Run the real seeder + a forecast against an Alembic-built database.

        Guards the case where the migration produces a schema that reflects
        identically but still rejects the application's writes.
        """
        import asyncio

        from picnic_meal_planner.db import engine as engine_mod
        from picnic_meal_planner.db.queries import get_all_product_stats, get_db
        from picnic_meal_planner.forecasting.engine import get_reorder_due
        from tests.fixtures.history import seed_database

        async def _exercise():
            engine_mod._engine = None
            engine_mod._session_factory = None
            try:
                async with get_db() as session:
                    await seed_database(session)
                async with get_db() as session:
                    stats = await get_all_product_stats(session)
                return get_reorder_due(stats)
            finally:
                if engine_mod._engine is not None:
                    await engine_mod._engine.dispose()
                engine_mod._engine = None
                engine_mod._session_factory = None

        # DB_PATH still points at migrated_db via the fixture's monkeypatch.
        overdue = asyncio.run(_exercise())
        assert {"p_milk_whole", "p_bread_whole"} <= {r["product_id"] for r in overdue}
