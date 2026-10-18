"""Tests for scripts/import_history.py: what a run writes and when it is done.

Picnic is replaced by in-memory fakes; the database is the per-test SQLite
file from conftest.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import func, select

from picnic_meal_planner.db import queries as db
from picnic_meal_planner.db.models import Order, OrderItem
from picnic_meal_planner.picnic import client as picnic

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_history.py"
_spec = importlib.util.spec_from_file_location("import_history", _SCRIPT)
import_history = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(import_history)


def _detail(did: str) -> dict:
    return {
        "id": did,
        "creation_time": "2026-07-14T10:00:00",
        "orders": [{"items": [{"id": "p_milk", "price": 119, "count": 2}]}],
    }


@pytest.fixture
def fake_picnic(monkeypatch):
    """Summaries and details the test can change between runs."""
    monkeypatch.setenv("IMPORT_DELAY_SECONDS", "0")
    state = {"summaries": [], "details": {}, "categories": {}}

    async def get_deliveries():
        return list(state["summaries"])

    async def get_delivery(did):
        detail = state["details"][did]
        if isinstance(detail, Exception):
            raise detail
        return detail

    async def get_product_category(product_id):
        category = state["categories"].get(product_id)
        if isinstance(category, Exception):
            raise category
        return category

    monkeypatch.setattr(picnic, "get_deliveries", get_deliveries)
    monkeypatch.setattr(picnic, "get_product_category", get_product_category)
    monkeypatch.setattr(picnic, "get_delivery", get_delivery)
    return state


async def _orders() -> dict[str, int]:
    async with db.get_db() as conn:
        rows = await conn.execute(
            select(Order.picnic_order_id, func.count(OrderItem.id))
            .outerjoin(OrderItem, OrderItem.order_id == Order.id)
            .group_by(Order.id)
        )
        return dict(rows.all())


async def _checkpoint() -> dict:
    async with db.get_db() as conn:
        return await db.get_latest_checkpoint(conn)


async def test_imports_completed_and_skips_cancelled(fake_picnic):
    fake_picnic["summaries"] = [
        {"id": "d2", "status": "COMPLETED"},
        {"id": "d1", "status": "CANCELLED"},
    ]
    fake_picnic["details"] = {"d2": _detail("d2")}
    await import_history.main()

    assert await _orders() == {"d2": 1}
    assert (await _checkpoint())["finished"]


async def test_current_delivery_is_imported_by_a_later_run(fake_picnic):
    fake_picnic["summaries"] = [
        {"id": "d3", "status": "CURRENT"},
        {"id": "d2", "status": "COMPLETED"},
    ]
    fake_picnic["details"] = {"d2": _detail("d2"), "d3": _detail("d3")}
    await import_history.main()

    assert await _orders() == {"d2": 1}
    checkpoint = await _checkpoint()
    assert not checkpoint["finished"]
    assert checkpoint["last_delivery_id"] is None   # never past the deferred one

    fake_picnic["summaries"][0]["status"] = "COMPLETED"
    await import_history.main()

    assert await _orders() == {"d2": 1, "d3": 1}    # d2 not doubled
    assert (await _checkpoint())["finished"]


async def test_error_detail_is_not_stored_and_is_retried(fake_picnic):
    fake_picnic["summaries"] = [{"id": "d1", "status": "COMPLETED"}]
    fake_picnic["details"] = {"d1": {"error": {"code": "DELIVERY_NOT_FOUND"}}}
    await import_history.main()

    assert await _orders() == {}
    assert not (await _checkpoint())["finished"]

    fake_picnic["details"]["d1"] = _detail("d1")
    await import_history.main()
    assert await _orders() == {"d1": 1}


async def test_fetch_failure_is_retried(fake_picnic):
    fake_picnic["summaries"] = [{"id": "d1", "status": "COMPLETED"}]
    fake_picnic["details"] = {"d1": ConnectionError("boom")}
    await import_history.main()
    assert not (await _checkpoint())["finished"]

    fake_picnic["details"]["d1"] = _detail("d1")
    await import_history.main()
    assert await _orders() == {"d1": 1}


async def _category(product_id: str) -> str | None:
    async with db.get_db() as conn:
        return (await db.get_product(conn, product_id))["category"]


async def test_categories_are_filled_in(fake_picnic):
    fake_picnic["summaries"] = [{"id": "d1", "status": "COMPLETED"}]
    fake_picnic["details"] = {"d1": _detail("d1")}
    fake_picnic["categories"] = {"p_milk": "Milch"}
    await import_history.main()
    assert await _orders() == {"d1": 1}
    assert await _category("p_milk") == "Milch"


async def test_categories_are_backfilled_after_a_finished_import(fake_picnic):
    fake_picnic["summaries"] = [{"id": "d1", "status": "COMPLETED"}]
    fake_picnic["details"] = {"d1": _detail("d1")}
    fake_picnic["categories"] = {"p_milk": ConnectionError("boom")}
    await import_history.main()
    assert (await _checkpoint())["finished"]
    assert await _category("p_milk") is None

    fake_picnic["categories"] = {"p_milk": "Milch"}
    await import_history.main()
    assert await _category("p_milk") == "Milch"


async def test_login_error_stops_the_backfill(fake_picnic, capsys):
    fake_picnic["summaries"] = [{"id": "d1", "status": "COMPLETED"}]
    fake_picnic["details"] = {"d1": _detail("d1")}
    fake_picnic["categories"] = {"p_milk": picnic.PicnicLoginRequired("log in")}
    await import_history.main()
    assert await _category("p_milk") is None
    assert "Stopped: log in" in capsys.readouterr().err
