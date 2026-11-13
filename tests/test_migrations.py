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

    def test_upgrade_head_creates_all_tables(self, migrated_db):
        tables = set(_schema_snapshot(migrated_db))
        assert tables == {
            "products",
            "orders",
            "order_items",
            "import_checkpoints",
            "conversation_messages",
            "chat_settings",
            "picnic_auth",
            "api_requests",
            "categories",
            "category_parents",
            "web_login_tokens",
        }

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
        "table",
        ["products", "orders", "order_items", "import_checkpoints", "api_requests"],
    )
    def test_same_columns(self, migrated_db, orm_db, table):
        migrated = _schema_snapshot(migrated_db)[table]["columns"]
        orm = _schema_snapshot(orm_db)[table]["columns"]
        assert migrated == orm, (
            f"Schema drift in {table!r}: the Alembic migration and the ORM "
            f"models disagree. Add a migration for the model change."
        )

    @pytest.mark.parametrize(
        "table",
        ["products", "orders", "order_items", "import_checkpoints", "api_requests"],
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


# ---------------------------------------------------------------------------
# 0003: per-chat model on an existing, populated database
# ---------------------------------------------------------------------------

class TestChatModelMigration:
    def _rows(self, db_file: Path):
        engine = create_engine(f"sqlite:///{db_file}")
        try:
            with engine.connect() as conn:
                return [dict(r._mapping) for r in conn.exec_driver_sql(
                    "SELECT * FROM chat_settings ORDER BY chat_id"
                )]
        finally:
            engine.dispose()

    def _seed_at_0002(self, tmp_path, monkeypatch) -> Path:
        db_file = tmp_path / "old.db"
        monkeypatch.setenv("DB_PATH", str(db_file))
        command.upgrade(_alembic_config(), "0002")
        engine = create_engine(f"sqlite:///{db_file}")
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO chat_settings (chat_id, persist_history, updated_at) "
                    "VALUES (7, 0, '2026-01-01T00:00:00')"
                )
        finally:
            engine.dispose()
        return db_file

    def test_upgrade_keeps_existing_settings_and_defaults_model_to_null(
        self, tmp_path, monkeypatch
    ):
        db_file = self._seed_at_0002(tmp_path, monkeypatch)
        command.upgrade(_alembic_config(), "head")
        [row] = self._rows(db_file)
        assert row["persist_history"] == 0
        assert row["model"] is None

    def test_downgrade_drops_only_the_model_column(self, tmp_path, monkeypatch):
        db_file = self._seed_at_0002(tmp_path, monkeypatch)
        command.upgrade(_alembic_config(), "head")
        command.downgrade(_alembic_config(), "0002")
        [row] = self._rows(db_file)
        assert "model" not in row
        assert row["persist_history"] == 0


# ---------------------------------------------------------------------------
# 0008: category ids replace the product's category name
# ---------------------------------------------------------------------------

class TestCategoryMigration:
    def _sql(self, db_file: Path, *statements: str) -> list[dict]:
        engine = create_engine(f"sqlite:///{db_file}")
        try:
            with engine.begin() as conn:
                rows = []
                for sql in statements:
                    result = conn.exec_driver_sql(sql)
                    if result.returns_rows:
                        rows = [dict(r._mapping) for r in result]
                return rows
        finally:
            engine.dispose()

    def _seed_at_0007(self, tmp_path, monkeypatch) -> Path:
        db_file = tmp_path / "old.db"
        monkeypatch.setenv("DB_PATH", str(db_file))
        command.upgrade(_alembic_config(), "0007")
        self._sql(
            db_file,
            "INSERT INTO products (id, name, category) VALUES ('p_a', 'A', 'Milch'), ('p_b', 'B', NULL)",
            "INSERT INTO orders (id, ordered_at) VALUES (1, '2026-01-01T00:00:00')",
            "INSERT INTO order_items (order_id, product_id, quantity) VALUES (1, 'p_a', 2)",
        )
        return db_file

    def test_upgrade_drops_the_name_and_keeps_the_rows(self, tmp_path, monkeypatch):
        db_file = self._seed_at_0007(tmp_path, monkeypatch)
        command.upgrade(_alembic_config(), "head")
        rows = self._sql(db_file, "SELECT * FROM products ORDER BY id")
        assert [(r["id"], r["category_id"]) for r in rows] == [("p_a", None), ("p_b", None)]
        assert "category" not in rows[0]
        assert self._sql(db_file, "SELECT product_id, quantity FROM order_items") == [
            {"product_id": "p_a", "quantity": 2},
        ]

    def test_downgrade_restores_names_from_categories(self, tmp_path, monkeypatch):
        db_file = self._seed_at_0007(tmp_path, monkeypatch)
        command.upgrade(_alembic_config(), "head")
        self._sql(
            db_file,
            "INSERT INTO categories (id, l2_id, l1_id, name) VALUES (3, 2, 1, 'Milch')",
            "UPDATE products SET category_id = 3 WHERE id = 'p_a'",
        )
        command.downgrade(_alembic_config(), "0007")
        rows = self._sql(db_file, "SELECT id, category FROM products ORDER BY id")
        assert rows == [{"id": "p_a", "category": "Milch"}, {"id": "p_b", "category": None}]
        tables = {r["name"] for r in self._sql(db_file, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "categories" not in tables


# ---------------------------------------------------------------------------
# 0009: names of the L1 and L2 categories
# ---------------------------------------------------------------------------

class TestCategoryParentsMigration:
    def _sql(self, db_file: Path, *statements: str) -> list[dict]:
        engine = create_engine(f"sqlite:///{db_file}")
        try:
            with engine.begin() as conn:
                rows = []
                for sql in statements:
                    result = conn.exec_driver_sql(sql)
                    if result.returns_rows:
                        rows = [dict(r._mapping) for r in result]
                return rows
        finally:
            engine.dispose()

    def test_fills_l1_ids_from_siblings_and_rolls_back(self, tmp_path, monkeypatch):
        db_file = tmp_path / "old.db"
        monkeypatch.setenv("DB_PATH", str(db_file))
        command.upgrade(_alembic_config(), "0008")
        self._sql(
            db_file,
            "INSERT INTO categories (id, l2_id, l1_id, name) VALUES "
            "(30, 20, 1, 'Pils'), (31, 20, NULL, 'Alkoholfrei'), (40, 21, NULL, 'Honig'), "
            "(50, 22, 1, 'A'), (51, 22, 5, 'B'), (52, 22, NULL, 'C')",
        )

        command.upgrade(_alembic_config(), "0009")
        rows = self._sql(db_file, "SELECT id, l1_id FROM categories ORDER BY id")
        # 40 has no sibling that knows its L1, and 52's siblings disagree.
        assert rows == [
            {"id": 30, "l1_id": 1}, {"id": 31, "l1_id": 1}, {"id": 40, "l1_id": None},
            {"id": 50, "l1_id": 1}, {"id": 51, "l1_id": 5}, {"id": 52, "l1_id": None},
        ]

        command.downgrade(_alembic_config(), "0008")
        tables = {r["name"] for r in self._sql(db_file, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "category_parents" not in tables
        assert self._sql(db_file, "SELECT l1_id FROM categories WHERE id = 31") == [{"l1_id": 1}]


class TestCategoryCheckedAtMigration:
    def test_adds_checked_at_and_rolls_back(self, tmp_path, monkeypatch):
        db_file = tmp_path / "old.db"
        monkeypatch.setenv("DB_PATH", str(db_file))
        command.upgrade(_alembic_config(), "0009")
        sql = TestCategoryParentsMigration._sql
        sql(
            None, db_file,
            "INSERT INTO categories (id, l2_id, l1_id, name) VALUES (30, 20, 1, 'Pils')",
            "INSERT INTO category_parents (level, id, name) VALUES (1, 1, 'Getraenke')",
        )

        command.upgrade(_alembic_config(), "0010")
        assert sql(None, db_file, "SELECT checked_at FROM categories") == [{"checked_at": None}]
        sql(None, db_file, "INSERT INTO category_parents (level, id, name, checked_at) "
                           "VALUES (2, 20, NULL, '2026-11-11')")

        command.downgrade(_alembic_config(), "0009")
        assert sql(None, db_file, "SELECT level, id, name FROM category_parents") == [
            {"level": 1, "id": 1, "name": "Getraenke"},
        ]
        columns = sql(None, db_file, "SELECT name FROM pragma_table_info('categories')")
        assert "checked_at" not in {c["name"] for c in columns}


# ---------------------------------------------------------------------------
# 0011: login links of the reporting UI
# ---------------------------------------------------------------------------

def test_login_tokens_migration_round_trip(tmp_path, monkeypatch):
    db_file = tmp_path / "old.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    sql = TestCategoryParentsMigration._sql
    tables = "SELECT name FROM sqlite_master WHERE name = 'web_login_tokens'"

    command.upgrade(_alembic_config(), "0011")
    sql(None, db_file, "INSERT INTO web_login_tokens (token_hash, user_id, expires_at) "
                       "VALUES ('h', 1, '2026-11-11')")
    assert sql(None, db_file, "SELECT used_at FROM web_login_tokens") == [{"used_at": None}]

    command.downgrade(_alembic_config(), "0010")
    assert sql(None, db_file, tables) == []


def test_schema_revision_is_the_alembic_head():
    """engine.SCHEMA_REVISION gates startup; it must move with every migration."""
    from alembic.script import ScriptDirectory

    from picnic_meal_planner.db.engine import SCHEMA_REVISION

    assert ScriptDirectory.from_config(_alembic_config()).get_current_head() == SCHEMA_REVISION
