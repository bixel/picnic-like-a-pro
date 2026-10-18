"""Stepwise, resumable import of Picnic delivery history into the local SQLite DB.

Safe to run multiple times — each delivery is looked up by its Picnic id
before anything is written, and skipped if already imported; the UNIQUE
constraint on picnic_order_id is only the backstop.

A delivery that cannot be imported yet (still CURRENT, or its detail could not
be fetched or was unusable) is deferred: the run does not mark the import
finished and does not move the checkpoint past it, so the next run picks it up.

Every run then looks up the category of each stored product that has none
yet (delivery data carries no categories), including runs that find no new
deliveries.  A product Picnic no longer lists is retried on the next run.

Usage:
    uv run python scripts/import_history.py

Environment variables (from .env):
    IMPORT_BATCH_SIZE     — deliveries fetched per batch (default 10)
    IMPORT_DELAY_SECONDS  — pause between batches (default 2)
    DB_PATH               — path to SQLite file (default data/picnic.db)
"""

from __future__ import annotations

import asyncio
import os
import sys

# Ensure the src package is importable when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv()

from picnic_meal_planner.db import queries as db
from picnic_meal_planner.db.engine import SCHEMA_REVISION, init_db, pending_migration
from picnic_meal_planner.picnic import client as picnic

# A cancelled delivery never arrived, so it is skipped for good.  A current one
# can still change: it is deferred, and imported by a later run once complete.
SKIP_STATUSES = {"CANCELLED"}
DEFER_STATUSES = {"CURRENT"}


async def main() -> None:
    stored = await pending_migration()
    if stored is not None:
        # Before init_db(), which would create the new tables outside Alembic.
        raise SystemExit(
            f"Database is at migration {stored}, the code needs {SCHEMA_REVISION}. "
            "Run `uv run alembic upgrade head` first."
        )
    await init_db()
    await import_deliveries()
    await backfill_categories()


async def backfill_categories() -> None:
    """Fill in the category of every stored product that has none."""
    batch_size = int(os.getenv("IMPORT_BATCH_SIZE", "10"))
    delay_seconds = float(os.getenv("IMPORT_DELAY_SECONDS", "2"))

    async with db.get_db() as conn:
        product_ids = await db.products_without_category(conn)
    if not product_ids:
        return
    print(f"\nLooking up categories for {len(product_ids)} product(s)...")

    found = 0
    login_error = None
    for i in range(0, len(product_ids), batch_size):
        categories: dict[str, str] = {}
        for product_id in product_ids[i : i + batch_size]:
            try:
                category = await picnic.get_product_category(product_id)
            except (picnic.PicnicLoginRequired, picnic.PicnicLoginPending) as exc:
                login_error = exc
                break
            except Exception as exc:  # noqa: BLE001 — retried by the next run
                print(f"  Warning: could not look up category of {product_id}: {exc}")
                continue
            if category:
                categories[product_id] = category

        async with db.get_db() as conn:
            for product_id, category in categories.items():
                await db.set_product_category(conn, product_id, category)
            await conn.commit()
        found += len(categories)
        if login_error:
            print(f"  Stopped: {login_error}", file=sys.stderr)
            break
        print(f"  {min(i + batch_size, len(product_ids))}/{len(product_ids)} checked", flush=True)

        if i + batch_size < len(product_ids):
            await asyncio.sleep(delay_seconds)

    print(f"Categories found for {found} of {len(product_ids)} product(s).")
    if found < len(product_ids):
        print("The rest will be retried on the next run.")


async def import_deliveries() -> None:
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
        print(f"Error fetching deliveries: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Run `uv run python scripts/preflight.py` to check the Picnic login.", file=sys.stderr)
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
        ids = [picnic.delivery_id(d) for d in all_deliveries]
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
    # Once a delivery is deferred, the checkpoint must not move past it, or a
    # resumed run would never see it again.
    deferred = 0

    def settled(did: str) -> None:
        nonlocal last_delivery_id
        if not deferred:
            last_delivery_id = did

    for i in range(0, len(remaining), batch_size):
        batch = remaining[i : i + batch_size]
        batch_num += 1
        start = already_imported + i + 1
        end = already_imported + i + len(batch)
        print(f"\nBatch {batch_num}/{num_batches} (deliveries {start}–{end})...", end="  ", flush=True)

        imported_in_batch = 0
        for delivery_summary in batch:
            delivery_id = picnic.delivery_id(delivery_summary)
            if not delivery_id:
                continue

            status = (delivery_summary.get("status") or "").upper()
            if status in SKIP_STATUSES:
                print(f"\n  Skipping {status.lower()} delivery {delivery_id}")
                settled(delivery_id)
                continue
            if status in DEFER_STATUSES:
                print(f"\n  Deferring {status.lower()} delivery {delivery_id} to a later run")
                deferred += 1
                continue

            async with db.get_db() as conn:
                already_there = await db.get_order_id(conn, delivery_id) is not None
            if already_there:
                # Committed by an earlier run that stopped before its batch
                # checkpoint; inserting its items again would double them.
                settled(delivery_id)
                continue

            try:
                detail = await picnic.get_delivery(delivery_id)
            except Exception as exc:
                print(f"\n  Warning: could not fetch delivery {delivery_id}: {exc}")
                deferred += 1
                continue

            # Parsed before opening a session: an unusable detail must leave
            # no row behind, or the check above would skip it forever.
            reason = picnic.delivery_unusable_reason(detail)
            if reason:
                print(f"\n  Warning: deferring delivery {delivery_id}: {reason}")
                deferred += 1
                continue
            fields = picnic.delivery_summary_fields(detail)
            items = picnic.delivery_line_items(detail)

            async with db.get_db() as conn:
                order_id = await db.insert_order(
                    conn,
                    picnic_order_id=delivery_id,
                    **fields,
                )

                for item in items:
                    await db.upsert_product(conn, item["product"])
                    await db.insert_order_item(
                        conn,
                        order_id=order_id,
                        product_id=item["product"]["id"],
                        quantity=item["quantity"],
                        unit_price=item["unit_price"],
                    )

                await conn.commit()

            imported_in_batch += 1
            settled(delivery_id)

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
            await asyncio.sleep(delay_seconds)
        else:
            print()

    # Mark as finished, unless something was deferred to the next run
    async with db.get_db() as conn:
        await db.save_checkpoint(
            conn,
            last_delivery_id=last_delivery_id,
            total_imported=total_imported,
            finished=not deferred,
        )

    print(f"\nDone. Total deliveries imported: {total_imported}")
    if deferred:
        print(f"{deferred} delivery(ies) deferred — run the import again later to pick them up.")


if __name__ == "__main__":
    asyncio.run(main())
