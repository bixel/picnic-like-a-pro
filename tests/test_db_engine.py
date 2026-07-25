"""Tests for db/engine.py and the ORM-level guarantees of db/models.py.

Covers behaviour the refactor introduced that line coverage alone would not
exercise meaningfully:

  * DB_PATH → connection URL mapping, and the lazy engine singleton (a stale
    engine would silently send writes to the wrong database file).
  * The SQLite ``PRAGMA foreign_keys=ON`` listener — off by default in SQLite,
    so without it the FK constraints in the models are decorative.
  * ON DELETE CASCADE from orders to order_items.
  * init_db() idempotency.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from picnic_meal_planner.db import engine as engine_mod
from picnic_meal_planner.db.models import Order, OrderItem, Product
from picnic_meal_planner.db.queries import get_db


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

class TestDbUrl:
    def test_uses_aiosqlite_driver(self, monkeypatch):
        monkeypatch.setenv("DB_PATH", "some/where.db")
        assert engine_mod._db_url() == "sqlite+aiosqlite:///some/where.db"

    def test_defaults_to_data_picnic_db(self, monkeypatch):
        monkeypatch.delenv("DB_PATH", raising=False)
        assert engine_mod._db_url() == "sqlite+aiosqlite:///data/picnic.db"

    def test_reflects_db_path_changes(self, monkeypatch):
        monkeypatch.setenv("DB_PATH", "a.db")
        first = engine_mod._db_url()
        monkeypatch.setenv("DB_PATH", "b.db")
        assert engine_mod._db_url() != first


# ---------------------------------------------------------------------------
# Engine / session factory singletons
# ---------------------------------------------------------------------------

class TestEngineSingleton:
    def test_get_engine_is_memoised(self, db_path):
        assert engine_mod.get_engine() is engine_mod.get_engine()

    def test_get_session_factory_is_memoised(self, db_path):
        assert engine_mod.get_session_factory() is engine_mod.get_session_factory()

    def test_engine_points_at_configured_db_path(self, db_path):
        assert db_path in str(engine_mod.get_engine().url)

    async def test_writes_land_in_the_configured_file(self, db_path):
        """A write through get_db() must be visible in the DB_PATH file."""
        from pathlib import Path

        async with get_db() as session:
            session.add(Product(id="p_x", name="X"))
            await session.commit()

        assert Path(db_path).exists()
        assert Path(db_path).stat().st_size > 0

        async with get_db() as session:
            assert await session.get(Product, "p_x") is not None


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    async def test_creates_all_four_tables(self, db_path):
        from sqlalchemy import inspect

        async with engine_mod.get_engine().connect() as conn:
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        assert {"products", "orders", "order_items", "import_checkpoints"} <= set(tables)

    async def test_is_idempotent(self, db_path):
        """Running init_db() twice must not raise or destroy existing data."""
        async with get_db() as session:
            session.add(Product(id="p_keep", name="Keep me"))
            await session.commit()

        await engine_mod.init_db()

        async with get_db() as session:
            assert await session.get(Product, "p_keep") is not None


# ---------------------------------------------------------------------------
# Foreign key enforcement (SQLite needs the PRAGMA to be set per connection)
# ---------------------------------------------------------------------------

class TestForeignKeyEnforcement:
    async def test_pragma_is_enabled_on_connection(self, db_path):
        from sqlalchemy import text

        async with get_db() as session:
            result = await session.execute(text("PRAGMA foreign_keys"))
            assert result.scalar() == 1

    async def test_order_item_requires_existing_product(self, db_path):
        async with get_db() as session:
            session.add(Order(id=1, ordered_at="2026-01-01T00:00:00+00:00"))
            await session.commit()
            session.add(
                OrderItem(order_id=1, product_id="p_does_not_exist", quantity=1)
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_order_item_requires_existing_order(self, db_path):
        async with get_db() as session:
            session.add(Product(id="p_real", name="Real"))
            await session.commit()
            session.add(OrderItem(order_id=999, product_id="p_real", quantity=1))
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_valid_order_item_is_accepted(self, db_path):
        async with get_db() as session:
            session.add(Product(id="p_real", name="Real"))
            session.add(Order(id=1, ordered_at="2026-01-01T00:00:00+00:00"))
            await session.commit()
            session.add(OrderItem(order_id=1, product_id="p_real", quantity=2))
            await session.commit()

            count = await session.scalar(select(func.count()).select_from(OrderItem))
            assert count == 1


# ---------------------------------------------------------------------------
# Cascade delete
# ---------------------------------------------------------------------------

class TestCascadeDelete:
    async def test_deleting_order_removes_its_items(self, db_path):
        async with get_db() as session:
            session.add(Product(id="p_c", name="Cascade"))
            session.add(Order(id=1, ordered_at="2026-01-01T00:00:00+00:00"))
            await session.commit()
            session.add(OrderItem(order_id=1, product_id="p_c", quantity=1))
            session.add(OrderItem(order_id=1, product_id="p_c", quantity=3))
            await session.commit()

            order = await session.get(Order, 1)
            await session.delete(order)
            await session.commit()

            assert await session.scalar(select(func.count()).select_from(OrderItem)) == 0

    async def test_deleting_order_leaves_products_intact(self, db_path):
        async with get_db() as session:
            session.add(Product(id="p_c", name="Cascade"))
            session.add(Order(id=1, ordered_at="2026-01-01T00:00:00+00:00"))
            await session.commit()
            session.add(OrderItem(order_id=1, product_id="p_c", quantity=1))
            await session.commit()

            await session.delete(await session.get(Order, 1))
            await session.commit()

            assert await session.get(Product, "p_c") is not None


# ---------------------------------------------------------------------------
# Package re-exports
# ---------------------------------------------------------------------------

class TestPackageExports:
    def test_db_package_exports_public_names(self):
        import picnic_meal_planner.db as db_pkg

        for name in (
            "Base", "Product", "Order", "OrderItem", "ImportCheckpoint",
            "get_db", "get_engine", "init_db",
        ):
            assert hasattr(db_pkg, name), f"db package should export {name}"

    async def test_engine_get_db_yields_a_working_session(self, db_path):
        """engine.get_db and queries.get_db are separate definitions; both work."""
        async with engine_mod.get_db() as session:
            session.add(Product(id="p_via_engine", name="Via engine"))
            await session.commit()
            assert await session.get(Product, "p_via_engine") is not None
