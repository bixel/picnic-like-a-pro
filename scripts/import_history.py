"""Stepwise, resumable import of Picnic delivery history into the local SQLite DB.

Safe to run multiple times — already-imported deliveries are skipped via
UNIQUE constraint on picnic_order_id.

Usage:
    uv run import-history

Environment variables (from .env):
    IMPORT_BATCH_SIZE     — deliveries fetched per batch (default 10)
    IMPORT_DELAY_SECONDS  — pause between batches (default 2)
    DB_PATH               — path to SQLite file (default data/picnic.db)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# Ensure the src package is importable when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv()

from picnic_meal_planner.db import queries as db
from picnic_meal_planner.picnic import client as picnic


async def main() -> None:
    batch_size = int(os.getenv("IMPORT_BATCH_SIZE", "10"))
    delay_seconds = float(os.getenv("IMPORT_DELAY_SECONDS", "2"))

    async with db.get_db() as conn:
        checkpoint = await db.get_latest_checkpoint(conn)

    if checkpoint and checkpoint["finished"]:
        print("Import already complete (checkpoint marked finished). Nothing to do.")
        return

    already_imported = checkpoint["total_imported"] if checkpoint else 0
    resume_after_id = checkpoint["last_delivery_id"] if checkpoint else None

    print("Fetching delivery list from Picnic...")
    try:
        all_deliveries = await picnic.get_deliveries()
    except Exception as exc:
        print(f"Error fetching deliveries: {exc}", file=sys.stderr)
        sys.exit(1)

    # all_deliveries is typically a list of delivery summary dicts
    if not isinstance(all_deliveries, list):
        print(f"Unexpected response format: {type(all_deliveries)}", file=sys.stderr)
        sys.exit(1)

    total = len(all_deliveries)
    print(f"Found {total} deliveries. {already_imported} already imported.")

    if already_imported >= total:
        print("Nothing new to import.")
        async with db.get_db() as conn:
            await db.save_checkpoint(conn, last_delivery_id=resume_after_id, total_imported=already_imported, finished=True)
        return

    # Skip already-imported deliveries if resuming
    remaining = all_deliveries
    if resume_after_id:
        ids = [d.get("id") or d.get("delivery_id") or d.get("order_id") for d in all_deliveries]
        try:
            skip_idx = ids.index(resume_after_id) + 1
            remaining = all_deliveries[skip_idx:]
            print(f"Resuming from checkpoint (last: {resume_after_id})")
        except ValueError:
            print(f"Checkpoint delivery ID {resume_after_id!r} not found in list; starting from beginning.")

    num_batches = (len(remaining) + batch_size - 1) // batch_size
    batch_num = 0
    last_delivery_id = resume_after_id
    total_imported = already_imported

    for i in range(0, len(remaining), batch_size):
        batch = remaining[i : i + batch_size]
        batch_num += 1
        start = already_imported + i + 1
        end = already_imported + i + len(batch)
        print(f"\nBatch {batch_num}/{num_batches} (deliveries {start}–{end})...", end="  ", flush=True)

        imported_in_batch = 0
        for delivery_summary in batch:
            delivery_id = (
                delivery_summary.get("id")
                or delivery_summary.get("delivery_id")
                or delivery_summary.get("order_id")
            )
            if not delivery_id:
                continue

            try:
                detail = await picnic.get_delivery(delivery_id)
            except Exception as exc:
                print(f"\n  Warning: could not fetch delivery {delivery_id}: {exc}")
                continue

            ordered_at = (
                detail.get("creation_time")
                or detail.get("ordered_at")
                or detail.get("slot", {}).get("window_start")
                or ""
            )
            delivered_at = (
                detail.get("delivery_time")
                or detail.get("delivered_at")
            )
            total_price = detail.get("total_price") or detail.get("price")

            async with db.get_db() as conn:
                order_id = await db.insert_order(
                    conn,
                    picnic_order_id=delivery_id,
                    ordered_at=ordered_at,
                    delivered_at=delivered_at,
                    total_price=total_price,
                )

                for order in detail.get("orders", [detail]):
                    for item in order.get("items", []):
                        product_raw = item.get("product") or item
                        product_id = product_raw.get("id") or product_raw.get("article_id")
                        if not product_id:
                            continue

                        await db.upsert_product(conn, {
                            "id": product_id,
                            "name": product_raw.get("name", ""),
                            "unit_price": product_raw.get("price") or product_raw.get("unit_price"),
                            "unit_quantity": product_raw.get("unit_quantity"),
                            "image_id": product_raw.get("image_id"),
                            "category": None,
                        })

                        quantity = item.get("count") or item.get("quantity") or 1
                        unit_price = (
                            item.get("unit_price")
                            or product_raw.get("price")
                        )
                        await db.insert_order_item(
                            conn,
                            order_id=order_id,
                            product_id=product_id,
                            quantity=quantity,
                            unit_price=unit_price,
                        )

                await conn.commit()

            imported_in_batch += 1
            last_delivery_id = delivery_id

        total_imported += imported_in_batch
        print(f"imported {imported_in_batch}", end="")

        # Save checkpoint after each batch
        async with db.get_db() as conn:
            await db.save_checkpoint(
                conn,
                last_delivery_id=last_delivery_id,
                total_imported=total_imported,
            )

        if i + batch_size < len(remaining):
            print(f"  [sleeping {delay_seconds}s]", flush=True)
            time.sleep(delay_seconds)
        else:
            print()

    # Mark as finished
    async with db.get_db() as conn:
        await db.save_checkpoint(
            conn,
            last_delivery_id=last_delivery_id,
            total_imported=total_imported,
            finished=True,
        )

    print(f"\nDone. Total deliveries imported: {total_imported}")


if __name__ == "__main__":
    asyncio.run(main())
