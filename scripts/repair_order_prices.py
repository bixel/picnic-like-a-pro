"""Repair ``order_items.unit_price`` and backfill ``products.unit_price``.

One-off repair for the wrong order-line-price bug (promotions ignored and
the 432199 placeholder stored for mixed promo lines).

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

# Ensure the src package is importable when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv()

from picnic_meal_planner import api_log
from picnic_meal_planner.db import queries as db
from picnic_meal_planner.db.engine import SchemaOutOfDateError, ensure_schema
from picnic_meal_planner.picnic import client as picnic

# Shared with import_history.py, defined next to PicnicRateLimited.
_STOP_ERRORS = picnic.STOP_ERRORS
_MAX_BACKOFF_SECONDS = picnic.MAX_BACKOFF_SECONDS
_with_backoff = picnic.with_backoff


async def repair_orders() -> bool:
    """Rewrite ``order_items.unit_price`` for every stored delivery.

    Returns ``True`` when a login or rate-limit stop ended the run early.
    """
    batch_size = int(os.getenv("IMPORT_BATCH_SIZE", "10"))
    delay_seconds = float(os.getenv("IMPORT_DELAY_SECONDS", "2"))

    async with db.get_db() as conn:
        imported_ids = await db.get_picnic_order_ids(conn)
    if not imported_ids:
        print("No orders to repair.")
        return False

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
                return True
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
                # A re-fetched delivery can name a product the order never stored
                # (or one never imported): create it first so the re-insert does
                # not hit the foreign key — as import_history does.
                for item in items:
                    await db.upsert_product(conn, item["product"])
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
    return False


async def backfill_product_prices() -> bool:
    """Write the current catalogue price of every ordered product.

    Returns ``True`` when a rate limit stopped the run early.  A ``None``
    lookup is not treated as "clear": it can mean the product is delisted *or*
    that its page was unreadable, so only a stored placeholder (4321.99 €) is
    cleared; a real price is left alone.
    """
    batch_size = int(os.getenv("IMPORT_BATCH_SIZE", "10"))
    delay_seconds = float(os.getenv("IMPORT_DELAY_SECONDS", "2"))

    async with db.get_db() as conn:
        product_ids = await db.ordered_product_ids(conn)
    if not product_ids:
        print("No products to price.")
        return False

    print(f"\nLooking up prices for {len(product_ids)} product(s)...")

    found = 0
    cleared = 0
    kept = 0
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

        if prices:
            async with db.get_db() as conn:
                for product_id, price in prices.items():
                    if price is not None:
                        await db.set_product_unit_price(conn, product_id, price)
                        found += 1
                        continue
                    # None means "delisted or unreadable", not free. Only clear a
                    # stored placeholder; a real price is left alone.
                    product = await db.get_product(conn, product_id)
                    if product and product["unit_price"] == picnic.PLACEHOLDER_PRICE:
                        await db.set_product_unit_price(conn, product_id, None)
                        cleared += 1
                    elif product and product["unit_price"] is not None:
                        kept += 1
                await conn.commit()

        print(f"  {min(i + batch_size, len(product_ids))}/{len(product_ids)} checked", flush=True)
        if stop:
            break
        if i + batch_size < len(product_ids):
            await asyncio.sleep(delay_seconds)

    print(
        f"Prices found for {found}, cleared {cleared} placeholder(s), "
        f"left {kept} unchanged of {len(product_ids)} product(s)."
    )
    return stop


async def main() -> None:
    try:
        await ensure_schema("the price repair")
    except SchemaOutOfDateError as exc:
        raise SystemExit(str(exc)) from None
    stopped = False
    async with api_log.request_context("repair_order_prices"):
        stopped = await repair_orders()
        if stopped:
            print("\nOrder repair stopped; skipping the price backfill.", file=sys.stderr)
        else:
            stopped = await backfill_product_prices()
    removed = await api_log.prune_expired()
    if removed:
        print(f"Pruned {removed} API request log row(s).")
    if stopped:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
