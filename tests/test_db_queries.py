"""Unit tests for db/queries.py – all database helper functions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from picnic_meal_planner.db import queries as db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


SAMPLE_PRODUCT = {
    "id": "p_test_milk",
    "name": "Test Milk 1L",
    "unit_price": 119,
    "unit_quantity": "1 liter",
    "image_id": "img_test_milk",
    "category": "Dairy",
}


class TestUpsertProduct:
    async def test_insert_new_product(self, db_conn):
        await db.upsert_product(db_conn, SAMPLE_PRODUCT)
        await db_conn.commit()
        row = await db.get_product(db_conn, "p_test_milk")
        assert row["name"] == "Test Milk 1L"
        assert row["unit_price"] == 119

    async def test_update_existing_product(self, db_conn):
        await db.upsert_product(db_conn, SAMPLE_PRODUCT)
        await db.upsert_product(db_conn, {**SAMPLE_PRODUCT, "name": "Updated", "unit_price": 129})
        await db_conn.commit()
        row = await db.get_product(db_conn, "p_test_milk")
        assert row["name"] == "Updated"
        assert row["unit_price"] == 129

    async def test_upsert_does_not_duplicate(self, db_conn):
        await db.upsert_product(db_conn, SAMPLE_PRODUCT)
        await db.upsert_product(db_conn, SAMPLE_PRODUCT)
        await db_conn.commit()
        async with db_conn.execute("SELECT COUNT(*) AS c FROM products") as cur:
            assert (await cur.fetchone())["c"] == 1

    async def test_upsert_product_with_none_fields(self, db_conn):
        await db.upsert_product(
            db_conn, {**SAMPLE_PRODUCT, "unit_price": None, "image_id": None, "category": None}
        )
        await db_conn.commit()
        assert (await db.get_product(db_conn, "p_test_milk"))["unit_price"] is None

    async def test_upsert_sets_last_seen_at(self, db_conn):
        await db.upsert_product(db_conn, SAMPLE_PRODUCT)
        await db_conn.commit()
        assert (await db.get_product(db_conn, "p_test_milk"))["last_seen_at"] is not None


class TestGetProduct:
    async def test_get_existing_product(self, db_conn):
        await db.upsert_product(db_conn, SAMPLE_PRODUCT)
        await db_conn.commit()
        assert (await db.get_product(db_conn, "p_test_milk"))["id"] == "p_test_milk"

    async def test_get_nonexistent_returns_none(self, db_conn):
        assert await db.get_product(db_conn, "p_does_not_exist") is None


class TestInsertOrder:
    async def test_insert_order_returns_id(self, db_conn):
        assert await db.insert_order(db_conn, ordered_at=_now_iso()) > 0

    async def test_insert_order_with_picnic_id(self, db_conn):
        oid = await db.insert_order(db_conn, picnic_order_id="picnic_001", ordered_at=_now_iso())
        assert oid > 0

    async def test_duplicate_picnic_id_returns_existing_id(self, db_conn):
        oid1 = await db.insert_order(db_conn, picnic_order_id="dup", ordered_at=_now_iso())
        await db_conn.commit()
        oid2 = await db.insert_order(db_conn, picnic_order_id="dup", ordered_at=_now_iso())
        assert oid1 == oid2

    async def test_duplicate_picnic_id_does_not_create_second_row(self, db_conn):
        await db.insert_order(db_conn, picnic_order_id="dup", ordered_at=_now_iso())
        await db_conn.commit()
        await db.insert_order(db_conn, picnic_order_id="dup", ordered_at=_now_iso())
        await db_conn.commit()
        async with db_conn.execute("SELECT COUNT(*) AS c FROM orders") as cur:
            assert (await cur.fetchone())["c"] == 1

    async def test_multiple_null_picnic_ids_allowed(self, db_conn):
        oid1 = await db.insert_order(db_conn, ordered_at=_now_iso())
        await db_conn.commit()
        oid2 = await db.insert_order(db_conn, ordered_at=_now_iso())
        await db_conn.commit()
        assert oid1 != oid2

    async def test_stores_optional_fields(self, db_conn):
        await db.insert_order(
            db_conn,
            picnic_order_id="full",
            ordered_at=_now_iso(),
            delivered_at=_now_iso(),
            total_price=1234,
            notes="a note",
        )
        await db_conn.commit()
        async with db_conn.execute("SELECT * FROM orders WHERE picnic_order_id='full'") as cur:
            row = await cur.fetchone()
        assert row["total_price"] == 1234
        assert row["notes"] == "a note"


class TestInsertOrderItem:
    async def test_insert_order_item(self, db_conn):
        await db.upsert_product(db_conn, SAMPLE_PRODUCT)
        oid = await db.insert_order(db_conn, ordered_at=_now_iso())
        await db.insert_order_item(
            db_conn, order_id=oid, product_id="p_test_milk", quantity=2, unit_price=119
        )
        await db_conn.commit()
        history = await db.get_order_history(db_conn, limit=1)
        assert history[0]["items"][0]["quantity"] == 2

    async def test_multiple_items_per_order(self, db_conn):
        await db.upsert_product(db_conn, SAMPLE_PRODUCT)
        await db.upsert_product(db_conn, {**SAMPLE_PRODUCT, "id": "p_test_bread", "name": "Bread"})
        oid = await db.insert_order(db_conn, ordered_at=_now_iso())
        await db.insert_order_item(db_conn, order_id=oid, product_id="p_test_milk", quantity=1)
        await db.insert_order_item(db_conn, order_id=oid, product_id="p_test_bread", quantity=3)
        await db_conn.commit()
        assert len((await db.get_order_history(db_conn, limit=1))[0]["items"]) == 2


class TestGetOrderHistory:
    async def test_empty_db_returns_empty_list(self, db_conn):
        assert await db.get_order_history(db_conn) == []

    async def test_returns_orders_with_items(self, seeded_db_conn):
        history = await db.get_order_history(seeded_db_conn, limit=5)
        assert len(history) == 5
        for order in history:
            assert "ordered_at" in order
            assert len(order["items"]) > 0

    async def test_limit_is_respected(self, seeded_db_conn):
        assert len(await db.get_order_history(seeded_db_conn, limit=3)) == 3

    async def test_orders_sorted_by_date_descending(self, seeded_db_conn):
        dates = [h["ordered_at"] for h in await db.get_order_history(seeded_db_conn, limit=25)]
        assert dates == sorted(dates, reverse=True)

    async def test_days_back_narrows_results(self, seeded_db_conn):
        recent = await db.get_order_history(seeded_db_conn, limit=50, days_back=5)
        all_orders = await db.get_order_history(seeded_db_conn, limit=50)
        assert len(recent) < len(all_orders)

    async def test_days_back_30_returns_about_4_orders(self, seeded_db_conn):
        # Newest order is 8 days ago, then 15, 22, 29 days ago
        assert 3 <= len(await db.get_order_history(seeded_db_conn, limit=50, days_back=30)) <= 5

    async def test_items_have_product_name(self, seeded_db_conn):
        for item in (await db.get_order_history(seeded_db_conn, limit=1))[0]["items"]:
            assert item["product_name"] is not None

    async def test_all_25_orders_retrievable(self, seeded_db_conn):
        assert len(await db.get_order_history(seeded_db_conn, limit=100)) == 25


class TestGetProductOrderStats:
    async def test_returns_none_for_unknown_product(self, seeded_db_conn):
        assert await db.get_product_order_stats(seeded_db_conn, "p_nonexistent") is None

    async def test_weekly_product_has_correct_order_count(self, seeded_db_conn):
        assert (await db.get_product_order_stats(seeded_db_conn, "p_milk_whole"))["order_count"] == 25

    async def test_weekly_product_interval_is_7_days(self, seeded_db_conn):
        stats = await db.get_product_order_stats(seeded_db_conn, "p_milk_whole")
        assert 6.5 <= stats["avg_interval_days"] <= 7.5

    async def test_biweekly_product_has_14_day_interval(self, seeded_db_conn):
        stats = await db.get_product_order_stats(seeded_db_conn, "p_coffee")
        assert 13.0 <= stats["avg_interval_days"] <= 15.0

    async def test_monthly_product_has_28_day_interval(self, seeded_db_conn):
        stats = await db.get_product_order_stats(seeded_db_conn, "p_spaghetti")
        assert 26.0 <= stats["avg_interval_days"] <= 30.0

    async def test_one_time_product_has_no_interval(self, seeded_db_conn):
        stats = await db.get_product_order_stats(seeded_db_conn, "p_tomato_sauce")
        assert stats["order_count"] == 1
        assert stats["avg_interval_days"] is None

    async def test_avg_quantity_reflects_seeded_pattern(self, seeded_db_conn):
        assert (await db.get_product_order_stats(seeded_db_conn, "p_milk_whole"))["avg_quantity"] == 2.0

    async def test_result_has_required_fields(self, seeded_db_conn):
        stats = await db.get_product_order_stats(seeded_db_conn, "p_milk_whole")
        assert {"product_id", "order_count", "avg_quantity",
                "avg_interval_days", "last_ordered_at"} <= stats.keys()


class TestGetFrequentlyOrdered:
    async def test_empty_db_returns_empty(self, db_conn):
        assert await db.get_frequently_ordered(db_conn) == []

    async def test_sorted_by_count_descending(self, seeded_db_conn):
        counts = [r["order_count"] for r in await db.get_frequently_ordered(seeded_db_conn, limit=20)]
        assert counts == sorted(counts, reverse=True)

    async def test_weekly_staples_at_top(self, seeded_db_conn):
        result = await db.get_frequently_ordered(seeded_db_conn, limit=20)
        assert "p_milk_whole" in {r["product_id"] for r in result[:5]}

    async def test_limit_respected(self, seeded_db_conn):
        assert len(await db.get_frequently_ordered(seeded_db_conn, limit=5)) <= 5

    async def test_result_has_required_fields(self, seeded_db_conn):
        for item in await db.get_frequently_ordered(seeded_db_conn, limit=5):
            assert {"product_id", "order_count", "avg_quantity", "last_ordered_at"} <= item.keys()


class TestGetAllProductStats:
    async def test_empty_db_returns_empty(self, db_conn):
        assert await db.get_all_product_stats(db_conn) == []

    async def test_requires_at_least_2_orders(self, seeded_db_conn):
        ids = {r["product_id"] for r in await db.get_all_product_stats(seeded_db_conn)}
        assert "p_tomato_sauce" not in ids

    async def test_includes_products_with_multiple_orders(self, seeded_db_conn):
        ids = {r["product_id"] for r in await db.get_all_product_stats(seeded_db_conn)}
        assert {"p_milk_whole", "p_coffee", "p_spaghetti"} <= ids

    async def test_includes_avg_interval_days(self, seeded_db_conn):
        """The forecasting engine depends on this column being present."""
        for row in await db.get_all_product_stats(seeded_db_conn):
            assert row["avg_interval_days"] is not None

    async def test_weekly_interval_correct(self, seeded_db_conn):
        stats = {r["product_id"]: r for r in await db.get_all_product_stats(seeded_db_conn)}
        assert 6.5 <= stats["p_milk_whole"]["avg_interval_days"] <= 7.5

    async def test_biweekly_interval_correct(self, seeded_db_conn):
        stats = {r["product_id"]: r for r in await db.get_all_product_stats(seeded_db_conn)}
        assert 13.0 <= stats["p_coffee"]["avg_interval_days"] <= 15.0

    async def test_result_has_required_fields(self, seeded_db_conn):
        for item in await db.get_all_product_stats(seeded_db_conn):
            assert {"product_id", "order_count", "avg_quantity", "last_ordered_at",
                    "first_ordered_at", "avg_interval_days"} <= item.keys()


class TestCheckpoints:
    async def test_no_checkpoint_returns_none(self, db_conn):
        assert await db.get_latest_checkpoint(db_conn) is None

    async def test_save_and_retrieve_checkpoint(self, db_conn):
        await db.save_checkpoint(db_conn, last_delivery_id="delivery_010", total_imported=10)
        result = await db.get_latest_checkpoint(db_conn)
        assert result["last_delivery_id"] == "delivery_010"
        assert result["total_imported"] == 10
        assert result["finished"] == 0

    async def test_finished_checkpoint(self, db_conn):
        await db.save_checkpoint(
            db_conn, last_delivery_id="delivery_025", total_imported=25, finished=True
        )
        assert (await db.get_latest_checkpoint(db_conn))["finished"] == 1

    async def test_latest_checkpoint_returns_most_recent(self, db_conn):
        await db.save_checkpoint(db_conn, last_delivery_id="d01", total_imported=1)
        await db.save_checkpoint(db_conn, last_delivery_id="d02", total_imported=2)
        assert (await db.get_latest_checkpoint(db_conn))["last_delivery_id"] == "d02"

    async def test_null_delivery_id_allowed(self, db_conn):
        await db.save_checkpoint(db_conn, last_delivery_id=None, total_imported=0)
        assert (await db.get_latest_checkpoint(db_conn))["last_delivery_id"] is None
