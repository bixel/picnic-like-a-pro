"""Repair ``order_items.unit_price`` and backfill ``products.unit_price``.

One-off repair for the order-price bug in ``ISSUE_ORDER_PRICES.md``.

The payloads of past deliveries are not stored, so every delivery is fetched
again (one ``get_delivery`` each) and its order lines are re-read with the
fixed :func:`~picnic_meal_planner.picnic.client.delivery_line_items`.  Each
order's lines are then rewritten (delete + re-insert), fixing the spent price
without touching quantities, order rows or category data.  Afterwards the
catalogue price of every ordered product is looked up from its product page and
written to ``products.unit_price``; a product Picnic no longer lists is cleared
to NULL rather than left at the placeholder value.

Safe to re-run: it only rewrites the two derived price columns.

Usage:
    uv run python scripts/repair_order_prices.py

Environment variables (from .env):
    IMPORT_BATCH_SIZE      — orders/products fetched per batch (default 10)
    IMPORT_DELAY_SECONDS   — pause between batches (default 2)
    IMPORT_BACKOFF_SECONDS — first wait after a rate limit (default 60, doubles)
    IMPORT_BACKOFF_RETRIES — retries after a rate limit before stopping (default 4)
    DB_PATH                — path to SQLite file (default data/picnic.db)
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable

# Ensure the src package is importable when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv()

from picnic_meal_planner import api_log
from picnic_meal_planner.db import queries as db
from picnic_meal_planner.db.engine import SchemaOutOfDateError, ensure_schema
from picnic_meal_planner.picnic import client as picnic

# A login or a rate limit that has exhausted its retries ends the run.
_STOP_ERRORS = (
    picnic.PicnicLoginRequired,
    picnic.PicnicLoginPending,
    picnic.PicnicRateLimited,
)

# A Retry-After beyond this ends the run instead of waiting.
_MAX_BACKOFF_SECONDS = 15 * 60

_sleep = asyncio.sleep


async def _with_backoff[T](func: Callable[..., Awaitable[T]], *args) -> T:
    """``await func(*args)``, waiting and retrying while Picnic rate limits."""
    retries = int(os.getenv("IMPORT_BACKOFF_RETRIES", "4"))
    base = float(os.getenv("IMPORT_BACKOFF_SECONDS", "60"))
    for attempt in range(retries + 1):
        try:
            return await func(*args)
        except picnic.PicnicRateLimited as exc:
            wait = exc.retry_after if exc.retry_after is not None else base * 2**attempt
            if attempt == retries or wait > _MAX_BACKOFF_SECONDS:
                raise
            print(f"  {exc} Waiting {wait:.0f} s (retry {attempt + 1}/{retries})...", flush=True)
            await _sleep(wait)
    raise AssertionError("unreachable")


async def repair_orders() -> None:
    """Rewrite ``order_items.unit_price`` for every stored delivery."""
    batch_size = int(os.getenv("IMPORT_BATCH_SIZE", "10"))
    delay_seconds = float(os.getenv("IMPORT_DELAY_SECONDS", "2"))

    async with db.get_db() as conn:
        imported_ids = await db.get_picnic_order_ids(conn)
    if not imported_ids:
        print("No orders to repair.")
        return

    order_ids = sorted(imported_ids)
    print(f"Repairing line prices for {len(order_ids)} delivery(ies)...")

    repaired = 0
    skipped = 0
    for i in range(0, len(order_ids), batch_size):
        batch = order_ids[i : i + batch_size]
        for picnic_id in batch:
            try:
                detail = await _with_backoff(picnic.get_delivery, picnic_id)
            except _STOP_ERRORS as exc:
                print(f"\n  Stopped: {exc}", file=sys.stderr)
                print(f"  Repaired {repaired}, skipped {skipped} of {len(order_ids)}.")
                return
            except Exception as exc:  # noqa: BLE001 — re-run the script to retry
                print(f"\n  Warning: could not fetch delivery {picnic_id}: {exc}")
                skipped += 1
                continue

            reason = picnic.delivery_unusable_reason(detail)
            if reason:
                print(f"\n  Warning: skipping delivery {picnic_id}: {reason}")
                skipped += 1
                continue

            items = picnic.delivery_line_items(detail)
            async with db.get_db() as conn:
                order_id = await db.get_order_id(conn, picnic_id)
                if order_id is None:
                    skipped += 1
                    continue
                await db.delete_order_items(conn, order_id)
                for item in items:
                    await db.insert_order_item(
                        conn,
                        order_id=order_id,
                        product_id=item["product"]["id"],
                        quantity=item["quantity"],
                        unit_price=item["unit_price"],
                    )
                await conn.commit()
            repaired += 1

        print(f"  {min(i + batch_size, len(order_ids))}/{len(order_ids)} repaired", flush=True)
        if i + batch_size < len(order_ids):
            await asyncio.sleep(delay_seconds)

    print(f"\nDone: repaired {repaired}, skipped {skipped} of {len(order_ids)} delivery(ies).")


async def backfill_product_prices() -> None:
    """Write the current catalogue price of every ordered product."""
    batch_size = int(os.getenv("IMPORT_BATCH_SIZE", "10"))
    delay_seconds = float(os.getenv("IMPORT_DELAY_SECONDS", "2"))

    async with db.get_db() as conn:
        product_ids = await db.ordered_product_ids(conn)
    if not product_ids:
        print("No products to price.")
        return

    print(f"\nLooking up prices for {len(product_ids)} product(s)...")

    found = 0
    cleared = 0
    stop = False
    for i in range(0, len(product_ids), batch_size):
        batch = product_ids[i : i + batch_size]
        prices: dict[str, int | None] = {}
        for product_id in batch:
            try:
                prices[product_id] = await _with_backoff(picnic.get_article_price, product_id)
            except _STOP_ERRORS as exc:
                print(f"\n  Stopped: {exc}", file=sys.stderr)
                stop = True
                break
            except Exception as exc:  # noqa: BLE001 — retried by a later run
                print(f"  Warning: could not look up price of {product_id}: {exc}")
                continue
            if prices[product_id] is not None:
                found += 1
            else:
                cleared += 1

        if prices:
            async with db.get_db() as conn:
                for product_id, price in prices.items():
                    await db.set_product_unit_price(conn, product_id, price)
                await conn.commit()

        print(f"  {min(i + batch_size, len(product_ids))}/{len(product_ids)} checked", flush=True)
        if stop:
            break
        if i + batch_size < len(product_ids):
            await asyncio.sleep(delay_seconds)

    print(f"Prices found for {found}, cleared for {cleared} of {len(product_ids)} product(s).")


async def main() -> None:
    try:
        await ensure_schema("the price repair")
    except SchemaOutOfDateError as exc:
        raise SystemExit(str(exc)) from None
    async with api_log.request_context("repair_order_prices"):
        await repair_orders()
        await backfill_product_prices()
    removed = await api_log.prune_expired()
    if removed:
        print(f"Pruned {removed} API request log row(s).")


if __name__ == "__main__":
    asyncio.run(main())
