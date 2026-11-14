"""Tests for scripts/repair_order_prices.py: the destructive rewrite paths.

Picnic is replaced by in-memory fakes; the database is the per-test SQLite file
from conftest.

The three ``xfail`` tests pin the known-unfixed blockers (the foreign-key crash,
the clear-to-NULL backfill, and the backfill running after a stop) so they show
up as xfailed instead of staying silently uncovered.  Remove each marker when
its blocker is fixed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from picnic_meal_planner.db import queries as db
from picnic_meal_planner.db.models import OrderItem
from picnic_meal_planner.picnic import client as picnic

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "repair_order_prices.py"
_spec = importlib.util.spec_from_file_location("repair_order_prices", _SCRIPT)
repair = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair)


def _detail(
    did: str, product_id: str = "p_milk", line_price: int = 300, quantity: int = 1
) -> dict:
    """A real-shaped delivery: a line price and one nested article."""
    return {
        "id": did,
        "creation_time": "2026-07-14T10:00:00",
        "orders": [{"items": [{
            "price": line_price,
            "items": [{
                "id": product_id,
                "price": 432199,   # the historical placeholder
                "decorators": [{"type": "QUANTITY", "quantity": quantity}],
            }],
        }]}],
    }


@pytest.fixture
def fake_picnic(monkeypatch):
    """Delivery details and article prices the test can change between calls."""
    monkeypatch.setenv("IMPORT_DELAY_SECONDS", "0")
    state = {
        "details": {}, "prices": {},
        "delivery_requests": [], "price_requests": [], "sleeps": [],
    }

    async def sleep(seconds):
        state["sleeps"].append(seconds)

    monkeypatch.setattr(repair, "_sleep", sleep)

    async def get_delivery(did):
        state["delivery_requests"].append(did)
        detail = state["details"][did]
        if isinstance(detail, Exception):
            raise detail
        return detail

    async def get_article_price(product_id):
        state["price_requests"].append(product_id)
        value = state["prices"].get(product_id)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(picnic, "get_delivery", get_delivery)
    monkeypatch.setattr(picnic, "get_article_price", get_article_price)
    return state


async def _seed_order(
    picnic_id: str,
    product_id: str,
    *,
    quantity: int = 1,
    unit_price: int = 999,
    catalogue_price: int | None = None,
) -> None:
    product = {"id": product_id, "name": product_id}
    if catalogue_price is not None:
        product["unit_price"] = catalogue_price
    async with db.get_db() as conn:
        await db.upsert_product(conn, product)
        order_id = await db.insert_order(
            conn,
            picnic_order_id=picnic_id,
            ordered_at="2026-01-01T00:00:00",
            total_price=unit_price,
        )
        await db.insert_order_item(
            conn,
            order_id=order_id,
            product_id=product_id,
            quantity=quantity,
            unit_price=unit_price,
        )
        await conn.commit()


async def _line_items(picnic_id: str) -> list[tuple]:
    async with db.get_db() as conn:
        order_id = await db.get_order_id(conn, picnic_id)
        rows = await conn.execute(
            select(OrderItem.product_id, OrderItem.quantity, OrderItem.unit_price)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.product_id)
        )
        return [tuple(row) for row in rows.all()]


# ---------------------------------------------------------------------------
# _with_backoff
# ---------------------------------------------------------------------------

async def test_backoff_waits_and_retries(fake_picnic, monkeypatch):
    monkeypatch.setenv("IMPORT_BACKOFF_SECONDS", "10")
    monkeypatch.setenv("IMPORT_BACKOFF_RETRIES", "3")
    attempts = {"n": 0}

    async def fetch():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise picnic.PicnicRateLimited(429)
        return "ok"

    assert await repair._with_backoff(fetch) == "ok"
    assert fake_picnic["sleeps"] == [10, 20]


async def test_backoff_gives_up_after_its_retries(fake_picnic, monkeypatch):
    monkeypatch.setenv("IMPORT_BACKOFF_SECONDS", "5")
    monkeypatch.setenv("IMPORT_BACKOFF_RETRIES", "1")

    async def fetch():
        raise picnic.PicnicRateLimited(429)

    with pytest.raises(picnic.PicnicRateLimited):
        await repair._with_backoff(fetch)
    assert fake_picnic["sleeps"] == [5]


async def test_backoff_honours_a_long_retry_after_without_waiting(fake_picnic):
    async def fetch():
        raise picnic.PicnicRateLimited(429, 3600)

    with pytest.raises(picnic.PicnicRateLimited):
        await repair._with_backoff(fetch)
    assert fake_picnic["sleeps"] == []


# ---------------------------------------------------------------------------
# repair_orders
# ---------------------------------------------------------------------------

async def test_rewrites_stored_line_prices(fake_picnic):
    await _seed_order("d1", "p_milk", unit_price=999)
    fake_picnic["details"] = {"d1": _detail("d1", line_price=300)}

    await repair.repair_orders()

    assert await _line_items("d1") == [("p_milk", 1, 300)]


async def test_skips_an_unusable_delivery(fake_picnic, capsys):
    await _seed_order("d1", "p_milk", unit_price=999)
    fake_picnic["details"] = {"d1": {"error": {"code": "DELIVERY_NOT_FOUND"}}}

    await repair.repair_orders()

    assert await _line_items("d1") == [("p_milk", 1, 999)]
    assert "skipping delivery d1" in capsys.readouterr().out


async def test_stops_on_a_login_error(fake_picnic, capsys):
    await _seed_order("d1", "p_milk", unit_price=999)
    fake_picnic["details"] = {"d1": picnic.PicnicLoginRequired("log in")}

    await repair.repair_orders()

    assert await _line_items("d1") == [("p_milk", 1, 999)]
    assert "Stopped: log in" in capsys.readouterr().err


async def test_skips_a_delivery_whose_fetch_fails(fake_picnic, capsys):
    await _seed_order("d1", "p_milk", unit_price=999)
    fake_picnic["details"] = {"d1": ConnectionError("boom")}

    await repair.repair_orders()

    assert await _line_items("d1") == [("p_milk", 1, 999)]
    assert "could not fetch delivery d1" in capsys.readouterr().out


@pytest.mark.xfail(
    reason="Blocker 2: repair re-inserts without upserting the product row",
    raises=IntegrityError,
)
async def test_a_missing_product_row_aborts_the_run(fake_picnic):
    # The stored order references p_old, but the re-fetched delivery now names
    # p_new, which has no products row — the re-insert hits the foreign key.
    await _seed_order("d1", "p_old", unit_price=999)
    fake_picnic["details"] = {"d1": _detail("d1", product_id="p_new", line_price=300)}

    await repair.repair_orders()


# ---------------------------------------------------------------------------
# backfill_product_prices
# ---------------------------------------------------------------------------

async def test_backfill_writes_the_found_price(fake_picnic):
    await _seed_order("d1", "p_a", unit_price=999, catalogue_price=777)
    fake_picnic["prices"] = {"p_a": 250}

    await repair.backfill_product_prices()

    async with db.get_db() as conn:
        assert (await db.get_product(conn, "p_a"))["unit_price"] == 250


async def test_backfill_stops_on_a_rate_limit(fake_picnic, monkeypatch, capsys):
    monkeypatch.setenv("IMPORT_BACKOFF_RETRIES", "0")
    await _seed_order("d1", "p_a", unit_price=999)
    await _seed_order("d2", "p_b", unit_price=999)
    fake_picnic["prices"] = {"p_a": picnic.PicnicRateLimited(429)}

    await repair.backfill_product_prices()

    assert fake_picnic["price_requests"] == ["p_a"]    # stopped before p_b
    assert "Stopped:" in capsys.readouterr().err


@pytest.mark.xfail(
    reason="Blocker 3: backfill clears a valid price when the lookup returns None"
)
async def test_backfill_keeps_a_valid_price_when_the_lookup_is_none(fake_picnic):
    await _seed_order("d1", "p_a", unit_price=999, catalogue_price=777)
    fake_picnic["prices"] = {"p_a": None}

    await repair.backfill_product_prices()

    async with db.get_db() as conn:
        assert (await db.get_product(conn, "p_a"))["unit_price"] == 777


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def test_main_repairs_orders_then_backfills_prices(fake_picnic):
    await _seed_order("d1", "p_milk", unit_price=999, catalogue_price=777)
    fake_picnic["details"] = {"d1": _detail("d1", line_price=300)}
    fake_picnic["prices"] = {"p_milk": 300}

    await repair.main()

    assert await _line_items("d1") == [("p_milk", 1, 300)]
    async with db.get_db() as conn:
        assert (await db.get_product(conn, "p_milk"))["unit_price"] == 300


@pytest.mark.xfail(
    reason="Blocker 1: main() backfills even after repair_orders() stopped"
)
async def test_main_skips_the_backfill_after_a_stop(fake_picnic):
    await _seed_order("d1", "p_milk", unit_price=999, catalogue_price=777)
    fake_picnic["details"] = {"d1": picnic.PicnicLoginRequired("log in")}
    fake_picnic["prices"] = {"p_milk": 300}

    await repair.main()

    assert fake_picnic["price_requests"] == []
