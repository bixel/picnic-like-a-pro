"""Tests for scripts/import_history.py: what a run writes and when it is done.

Picnic is replaced by in-memory fakes; the database is the per-test SQLite
file from conftest.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import requests
from sqlalchemy import func, select, update

from picnic_meal_planner.db import queries as db
from picnic_meal_planner.db.models import (
    Category,
    CategoryParent,
    ImportCheckpoint,
    Order,
    OrderItem,
)
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
    # categories: product id -> L3 name (ids 1/2/3), (l1, l2, l3) ids, or an
    # exception; a list is answered one entry per request.  pages: (l2, l3) ->
    # {l3: name} on that L2 page, or an exception; unset pages name the L3
    # given as a string in categories.  l1_pages: l1 id -> (l1 name, {l2:
    # name}) or an exception.  *_requests record what was asked, sleeps every
    # backoff wait.
    state = {
        "summaries": [], "details": {}, "categories": {}, "pages": {}, "l2_requests": [],
        "l1_pages": {}, "l1_requests": [], "category_requests": [], "sleeps": [],
    }

    async def sleep(seconds):
        state["sleeps"].append(seconds)

    monkeypatch.setattr(import_history, "_sleep", sleep)

    async def get_deliveries():
        return list(state["summaries"])

    async def get_delivery(did):
        detail = state["details"][did]
        if isinstance(detail, Exception):
            raise detail
        return detail

    async def get_category_ids(product_id):
        state["category_requests"].append(product_id)
        category = state["categories"].get(product_id)
        if isinstance(category, list):
            category = category.pop(0)
        if isinstance(category, Exception):
            raise category
        if isinstance(category, str):
            state["pages"].setdefault((2, 3), {3: category})
            return (1, 2, 3)
        return category

    async def get_l3_names(l2_id, l3_id):
        state["l2_requests"].append((l2_id, l3_id))
        page = state["pages"].get((l2_id, l3_id), {})
        if isinstance(page, Exception):
            raise page
        return dict(page)

    async def get_l1_names(l1_id):
        state["l1_requests"].append(l1_id)
        page = state["l1_pages"].get(l1_id, (None, {}))
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(picnic, "get_l1_names", get_l1_names)
    monkeypatch.setattr(picnic, "get_deliveries", get_deliveries)
    monkeypatch.setattr(picnic, "get_category_ids", get_category_ids)
    monkeypatch.setattr(picnic, "get_l3_names", get_l3_names)
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
    assert checkpoint["total_imported"] == 1

    fake_picnic["summaries"][0]["status"] = "COMPLETED"
    await import_history.main()

    assert await _orders() == {"d2": 1, "d3": 1}    # d2 not doubled
    assert (await _checkpoint())["finished"]


async def test_new_delivery_is_imported_after_a_finished_run(fake_picnic, capsys):
    fake_picnic["summaries"] = [
        {"id": "d1", "status": "COMPLETED"},
        {"id": "d0", "status": "COMPLETED"},
    ]
    fake_picnic["details"] = {"d0": _detail("d0"), "d1": _detail("d1")}
    await import_history.main()
    assert await _orders() == {"d0": 1, "d1": 1}
    assert (await _checkpoint())["finished"]

    # A delivery placed since the last run arrives at the top of the list, as
    # Picnic returns it, and the older ones are not fetched again.
    fake_picnic["summaries"].insert(0, {"id": "d2", "status": "COMPLETED"})
    fake_picnic["details"] = {"d2": _detail("d2")}
    capsys.readouterr()
    await import_history.main()

    assert "Imported 1 new delivery(ies); 3 in the database." in capsys.readouterr().out
    assert await _orders() == {"d0": 1, "d1": 1, "d2": 1}
    checkpoint = await _checkpoint()
    assert checkpoint["finished"]
    assert checkpoint["total_imported"] == 3


async def test_nothing_new_is_reported_without_fetching_details(fake_picnic, capsys):
    fake_picnic["summaries"] = [{"id": "d1", "status": "COMPLETED"}]
    fake_picnic["details"] = {"d1": _detail("d1")}
    await import_history.main()

    fake_picnic["details"] = {}   # a second fetch of d1 would raise KeyError
    await import_history.main()

    assert "Nothing new to import." in capsys.readouterr().out
    assert await _orders() == {"d1": 1}


async def test_cancelled_delivery_does_not_count_as_new(fake_picnic, capsys):
    fake_picnic["summaries"] = [
        {"id": "d2", "status": "COMPLETED"},
        {"id": "d1", "status": "CANCELLED"},
    ]
    fake_picnic["details"] = {"d2": _detail("d2")}
    await import_history.main()

    fake_picnic["details"] = {}   # any fetch would raise KeyError
    capsys.readouterr()
    await import_history.main()

    out = capsys.readouterr().out
    assert "0 to import, 1 cancelled" in out
    assert "Nothing new to import." in out
    assert await _orders() == {"d2": 1}


async def test_idle_run_writes_no_checkpoint(fake_picnic):
    fake_picnic["summaries"] = [{"id": "d1", "status": "COMPLETED"}]
    fake_picnic["details"] = {"d1": _detail("d1")}
    await import_history.main()
    first = await _checkpoint()

    await import_history.main()

    async with db.get_db() as conn:
        rows = await conn.scalar(select(func.count(ImportCheckpoint.id)))
    assert (await _checkpoint())["id"] == first["id"]
    assert rows == 2   # one batch checkpoint + the finished one, both from run 1


async def test_only_in_progress_left_is_nothing_new(fake_picnic, capsys):
    fake_picnic["summaries"] = [
        {"id": "d2", "status": "CURRENT"},
        {"id": "d1", "status": "COMPLETED"},
    ]
    fake_picnic["details"] = {"d1": _detail("d1")}
    await import_history.main()
    assert not (await _checkpoint())["finished"]

    capsys.readouterr()
    await import_history.main()

    out = capsys.readouterr().out
    assert "Nothing new to import." in out
    assert "1 delivery(ies) still in progress" in out
    assert not (await _checkpoint())["finished"]   # never claimed finished


async def test_error_detail_is_not_stored_and_is_retried(fake_picnic):
    fake_picnic["summaries"] = [{"id": "d1", "status": "COMPLETED"}]
    fake_picnic["details"] = {"d1": {"error": {"code": "DELIVERY_NOT_FOUND"}}}
    await import_history.main()

    assert await _orders() == {}
    assert await _checkpoint() is None   # nothing imported, nothing logged

    fake_picnic["details"]["d1"] = _detail("d1")
    await import_history.main()
    assert await _orders() == {"d1": 1}


async def test_fetch_failure_is_retried(fake_picnic):
    fake_picnic["summaries"] = [{"id": "d1", "status": "COMPLETED"}]
    fake_picnic["details"] = {"d1": ConnectionError("boom")}
    await import_history.main()
    assert await _orders() == {}
    assert await _checkpoint() is None

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
    async with db.get_db() as conn:
        assert (await db.get_product(conn, "p_milk"))["category_id"] == 3
        assert await db.get_category_names(conn) == {3: "Milch"}


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


def _not_json() -> requests.exceptions.JSONDecodeError:
    return requests.exceptions.JSONDecodeError("Expecting value", "<html>404</html>", 0)


def _limited(retry_after=None) -> picnic.PicnicRateLimited:
    return picnic.PicnicRateLimited(429, retry_after)


async def test_a_page_that_is_not_json_skips_only_that_product(fake_picnic, capsys):
    await _seed_products("p_a", "p_b", "p_c")
    fake_picnic["categories"] = {"p_a": (1, 20, 30), "p_b": _not_json(), "p_c": (1, 20, 30)}
    fake_picnic["pages"] = {(20, 30): {30: "Pils"}}
    assert await import_history.backfill_categories()

    assert fake_picnic["category_requests"] == ["p_a", "p_b", "p_c"]
    assert (await _category("p_a"), await _category("p_c")) == ("Pils", "Pils")
    assert "could not look up category of p_b" in capsys.readouterr().out


async def test_a_rate_limit_waits_and_retries(fake_picnic):
    await _seed_products("p_a")
    fake_picnic["categories"] = {"p_a": [_limited(), _limited(7), (1, 20, 30)]}
    fake_picnic["pages"] = {(20, 30): {30: "Pils"}}
    assert await import_history.backfill_categories()

    # IMPORT_BACKOFF_SECONDS (60) first, then Picnic's Retry-After.
    assert fake_picnic["sleeps"] == [60, 7]
    assert await _category("p_a") == "Pils"


async def test_a_lasting_rate_limit_stops_every_category_lookup(fake_picnic, monkeypatch, capsys):
    monkeypatch.setenv("IMPORT_BACKOFF_RETRIES", "2")
    monkeypatch.setenv("IMPORT_BACKOFF_SECONDS", "10")
    fake_picnic["summaries"] = [{"id": "d1", "status": "COMPLETED"}]
    fake_picnic["details"] = {"d1": _detail("d1")}
    await _seed_products("p_a", "p_b")
    fake_picnic["categories"] = {"p_a": (1, 20, 30), "p_b": _limited()}
    fake_picnic["pages"] = {(20, 30): {30: "Pils"}}
    await import_history.main()

    assert fake_picnic["sleeps"] == [10, 20]
    assert fake_picnic["category_requests"] == ["p_a", "p_b", "p_b", "p_b"]
    assert await _category("p_a") == "Pils"     # what came before the block is kept
    assert "Stopped: Picnic rate limited" in capsys.readouterr().err
    # Neither the parent names nor the refresh ask a blocking server.
    assert fake_picnic["l1_requests"] == []
    assert fake_picnic["l2_requests"] == [(20, 30)]


async def test_a_long_retry_after_stops_without_waiting(fake_picnic, capsys):
    await _seed_products("p_a")
    fake_picnic["categories"] = {"p_a": _limited(3600)}
    assert not await import_history.backfill_categories()
    assert fake_picnic["sleeps"] == []
    assert "retry after 3600 s" in capsys.readouterr().err


async def test_siblings_get_the_l1_of_the_product_that_found_them(fake_picnic):
    await _seed_products("p_a")
    fake_picnic["categories"] = {"p_a": (1, 20, 30)}
    fake_picnic["pages"] = {(20, 30): {30: "Pils", 31: "Alkoholfrei"}}
    await import_history.backfill_categories()
    async with db.get_db() as conn:
        assert (await conn.get(Category, 31)).l1_id == 1


async def test_parent_names_are_filled_in_once(fake_picnic):
    await _seed_products("p_a")
    fake_picnic["categories"] = {"p_a": (1, 20, 30)}
    fake_picnic["pages"] = {(20, 30): {30: "Pils"}}
    fake_picnic["l1_pages"] = {1: ("Getraenke", {20: "Bier", 21: "Wein"})}
    await import_history.backfill_categories()
    await import_history.backfill_parent_names()

    async with db.get_db() as conn:
        [listed] = await db.get_categories(conn)
    assert (listed["l1_name"], listed["l2_name"]) == ("Getraenke", "Bier")

    await import_history.backfill_parent_names()
    assert fake_picnic["l1_requests"] == [1]


async def _l1_ids_due() -> list[int]:
    async with db.get_db() as conn:
        return list(await db.l1_ids_due(conn, retry_before=import_history._now()))


async def test_a_parent_lookup_failure_is_retried_and_a_block_stops(fake_picnic, capsys):
    await _seed_products("p_a", "p_b")
    fake_picnic["categories"] = {"p_a": (1, 20, 30), "p_b": (2, 21, 31)}
    fake_picnic["pages"] = {(20, 30): {30: "Pils"}, (21, 31): {31: "Rot"}}
    await import_history.backfill_categories()

    fake_picnic["l1_pages"] = {1: ConnectionError("boom"), 2: ("Wein", {21: "Rotwein"})}
    assert await import_history.backfill_parent_names()
    assert fake_picnic["l1_requests"] == [1, 2]
    assert await _l1_ids_due() == [1]

    fake_picnic["l1_requests"].clear()
    fake_picnic["l1_pages"][1] = picnic.PicnicRateLimited(403, 3600)
    assert not await import_history.backfill_parent_names()
    assert "Stopped: Picnic blocked" in capsys.readouterr().err


async def _age_checks(days: int) -> None:
    """Make every stored check *days* older."""
    from datetime import UTC, datetime, timedelta

    then = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    async with db.get_db() as conn:
        await conn.execute(update(Category).values(checked_at=then))
        await conn.execute(update(CategoryParent).values(checked_at=then))
        await conn.commit()


async def test_a_page_without_names_is_retried_only_after_a_while(fake_picnic):
    await _seed_products("p_a")
    fake_picnic["categories"] = {"p_a": (1, 20, 30)}
    fake_picnic["pages"] = {(20, 30): {30: "Pils"}}
    await import_history.backfill_categories()

    # No title, and L2 20 is not listed.
    fake_picnic["l1_pages"] = {1: (None, {21: "Wein"})}
    await import_history.backfill_parent_names()
    await import_history.backfill_parent_names()
    assert fake_picnic["l1_requests"] == [1]

    await _age_checks(8)   # past CATEGORY_RETRY_DAYS (7)
    fake_picnic["l1_pages"] = {1: ("Getraenke", {20: "Bier"})}
    await import_history.backfill_parent_names()
    assert fake_picnic["l1_requests"] == [1, 1]
    async with db.get_db() as conn:
        [listed] = await db.get_categories(conn)
    assert (listed["l1_name"], listed["l2_name"]) == ("Getraenke", "Bier")


async def test_stale_names_are_refreshed(fake_picnic, monkeypatch):
    await _seed_products("p_a")
    fake_picnic["categories"] = {"p_a": (1, 20, 30)}
    fake_picnic["pages"] = {(20, 30): {30: "Pils", 31: "Alkoholfrei"}}
    fake_picnic["l1_pages"] = {1: ("Getraenke", {20: "Bier"})}
    await import_history.main()
    assert fake_picnic["l2_requests"] == [(20, 30)]   # fresh: no refresh yet

    await _age_checks(31)  # past CATEGORY_REFRESH_DAYS (30)
    fake_picnic["pages"] = {(20, 30): {30: "Pilsener", 32: "Radler"}}
    fake_picnic["l1_pages"] = {1: ("Getränke", {20: "Biere"})}
    await import_history.main()

    async with db.get_db() as conn:
        [listed] = await db.get_categories(conn)
        names = await db.get_category_names(conn)
        radler = await conn.get(Category, 32)
    assert (listed["l1_name"], listed["l2_name"], listed["name"]) == ("Getränke", "Biere", "Pilsener")
    # A name the page no longer lists is kept; a new sibling gets the L1.
    assert names == {30: "Pilsener", 31: "Alkoholfrei", 32: "Radler"}
    assert radler.l1_id == 1

    fake_picnic["l2_requests"].clear()
    fake_picnic["l1_requests"].clear()
    await import_history.main()
    assert (fake_picnic["l2_requests"], fake_picnic["l1_requests"]) == ([], [])


async def test_the_refresh_is_limited_per_run(fake_picnic, monkeypatch):
    monkeypatch.setenv("CATEGORY_REFRESH_LIMIT", "1")
    await _seed_products("p_a", "p_b")
    fake_picnic["categories"] = {"p_a": (1, 20, 30), "p_b": (1, 21, 31)}
    fake_picnic["pages"] = {(20, 30): {30: "Pils"}, (21, 31): {31: "Rot"}}
    await import_history.backfill_categories()
    await _age_checks(31)

    fake_picnic["l2_requests"].clear()
    await import_history.refresh_category_names()
    await import_history.refresh_category_names()
    assert sorted(fake_picnic["l2_requests"]) == [(20, 30), (21, 31)]


async def test_no_refresh_when_disabled(fake_picnic, monkeypatch):
    monkeypatch.setenv("CATEGORY_REFRESH_DAYS", "0")
    await _seed_products("p_a")
    fake_picnic["categories"] = {"p_a": (1, 20, 30)}
    fake_picnic["pages"] = {(20, 30): {30: "Pils"}}
    await import_history.backfill_categories()
    await _age_checks(365)
    fake_picnic["l2_requests"].clear()
    await import_history.refresh_category_names()
    assert fake_picnic["l2_requests"] == []


async def test_all_picnic_requests_of_a_run_share_one_log_batch(monkeypatch):
    """The real ``_call`` path is used, so the log rows are the script's own."""

    class FakeApi:
        def get_deliveries(self):
            return [{"id": "d1", "status": "COMPLETED"}]

        def get_delivery(self, delivery_id):
            return _detail(delivery_id)

        def get_article(self, product_id, add_category=False):
            return {"category": {"name": "Melk"}} if add_category else {}

    monkeypatch.setattr(picnic, "_client", FakeApi())
    await import_history.main()

    async with db.get_db() as conn:
        rows = await db.list_api_requests(conn, source="import_history")

    assert {r["operation"] for r in rows} == {
        "get_deliveries",
        "get_delivery",
        "get_article",
    }
    assert all(r["status"] == "success" for r in rows)
    assert all(r["request_body"] and r["response_body"] for r in rows)
    assert len({r["run_id"] for r in rows}) == 1
    assert all(r["run_id"] for r in rows)


async def test_a_run_prunes_expired_log_rows(fake_picnic, monkeypatch):
    monkeypatch.setenv("API_LOG_RETENTION_DAYS", "30")
    async with db.get_db() as conn:
        await db.insert_api_request(
            conn,
            provider="picnic",
            operation="ancient",
            status="success",
            duration_ms=1,
            requested_at="2020-01-01T00:00:00+00:00",
        )
        await conn.commit()

    await import_history.main()

    async with db.get_db() as conn:
        rows = await db.list_api_requests(conn)
    assert "ancient" not in {r["operation"] for r in rows}


async def _seed_products(*product_ids: str) -> None:
    async with db.get_db() as conn:
        for pid in product_ids:
            await db.upsert_product(conn, {"id": pid, "name": pid})
        await conn.commit()


async def test_one_l2_page_names_its_siblings_across_runs(fake_picnic):
    await _seed_products("p_beer", "p_beer2", "p_pils")
    fake_picnic["categories"] = {
        "p_beer": (1, 20, 30), "p_beer2": (1, 20, 30), "p_pils": (1, 20, 31),
    }
    fake_picnic["pages"] = {(20, 30): {30: "Alkoholfrei", 31: "Pils"}}
    await import_history.backfill_categories()

    assert fake_picnic["l2_requests"] == [(20, 30)]
    assert await _category("p_beer2") == "Alkoholfrei"
    assert await _category("p_pils") == "Pils"

    # A later run finds the sibling names in the database.
    await _seed_products("p_pils2")
    fake_picnic["categories"]["p_pils2"] = (1, 20, 31)
    fake_picnic["l2_requests"].clear()
    await import_history.backfill_categories()
    assert fake_picnic["l2_requests"] == []
    assert await _category("p_pils2") == "Pils"


async def test_an_l3_missing_from_its_page_is_fetched_once_per_run(fake_picnic):
    await _seed_products("p_a", "p_b")
    fake_picnic["categories"] = {"p_a": (1, 20, 30), "p_b": (1, 20, 30)}
    fake_picnic["pages"] = {(20, 30): {31: "Pils"}}
    await import_history.backfill_categories()

    assert fake_picnic["l2_requests"] == [(20, 30)]
    assert await _category("p_a") is None
    async with db.get_db() as conn:
        assert await db.products_without_category(conn) == ["p_a", "p_b"]
        assert await db.get_category_names(conn) == {31: "Pils"}


async def test_an_l2_page_error_skips_only_that_product(fake_picnic):
    await _seed_products("p_a", "p_b")
    fake_picnic["categories"] = {"p_a": (1, 20, 30), "p_b": (1, 21, 32)}
    fake_picnic["pages"] = {(20, 30): ConnectionError("boom"), (21, 32): {32: "Wein"}}
    await import_history.backfill_categories()
    assert await _category("p_a") is None
    assert await _category("p_b") == "Wein"
