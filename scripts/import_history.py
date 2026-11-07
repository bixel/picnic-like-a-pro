"""Import Picnic delivery history into the local SQLite DB, resumably.

Safe to run any number of times, including on a schedule: what needs importing
is decided by what the ``orders`` table already holds, not by how far an
earlier run got, so each run adds the deliveries that are new since the last
one and leaves the rest alone.  Each delivery is looked up by its Picnic id
before anything is written; the UNIQUE constraint on picnic_order_id is only
the backstop.

Picnic returns the whole delivery list with the newest first, so a new delivery
arrives *above* everything already imported — which is why the checkpoint's
``last_delivery_id`` (the newest delivery a run imported) is a progress log
only and never a resume position.  A checkpoint row is written only by runs
that import something; an idle run writes nothing.

Cancelled deliveries are left out of the work list.  A delivery that cannot be
imported yet (still CURRENT, or its detail could not be fetched or was
unusable) is deferred: it has no row in ``orders``, so a later run finds it
again, and the run's checkpoint is not marked finished.

Every run then looks up the category (its L1/L2/L3 ids and L3 name) of each
stored product whose category id is not known yet (delivery data carries no
categories), including runs that find no new deliveries.  That costs one
request per product, plus one per L2 category page whose L3 names are not
known yet.  A product Picnic no longer lists is retried on the next run.

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
    """Fill in the category of every stored product without a category id."""
    batch_size = int(os.getenv("IMPORT_BATCH_SIZE", "10"))
    delay_seconds = float(os.getenv("IMPORT_DELAY_SECONDS", "2"))

    async with db.get_db() as conn:
        product_ids = await db.products_without_category(conn)
        # {l3_id: name}; grows with every L2 page fetched, so a product whose
        # L3 is already named costs one request instead of two.
        known_names = await db.get_category_names(conn)
    if not product_ids:
        return
    print(f"\nLooking up categories for {len(product_ids)} product(s)...")

    found = 0
    login_error = None
    for i in range(0, len(product_ids), batch_size):
        categories: dict[str, dict] = {}
        for product_id in product_ids[i : i + batch_size]:
            try:
                category = await picnic.get_product_category(product_id, known_names)
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

    async with db.get_db() as conn:
        imported_ids = await db.get_picnic_order_ids(conn)

    # What to import is decided by `orders`, not by the checkpoint: Picnic hands
    # back the whole list newest-first, so a delivery ordered since the last run
    # sits above everything that run imported.  This also covers a run that was
    # interrupted between a commit and its batch checkpoint.
    #
    # Statuses are sorted out here rather than in the loop: a cancelled
    # delivery never gets a row in `orders`, so if it went into `pending` it
    # would be "new" on every run and the run would never find nothing to do.
    pending: list[dict] = []
    cancelled = 0
    deferred = 0   # still CURRENT now; failed fetches are added in the loop
    for summary in all_deliveries:
        delivery_id = picnic.delivery_id(summary)
        if not delivery_id or delivery_id in imported_ids:
            continue
        status = (summary.get("status") or "").upper()
        if status in SKIP_STATUSES:
            cancelled += 1
        elif status in DEFER_STATUSES:
            deferred += 1
        else:
            pending.append(summary)

    print(
        f"Found {len(all_deliveries)} deliveries: {len(imported_ids)} already imported, "
        f"{len(pending)} to import, {cancelled} cancelled, {deferred} still in progress."
    )

    if not pending:
        # No checkpoint row: an idle run (e.g. a scheduled one) leaves the
        # database untouched, so `import_checkpoints` only grows with imports.
        print("Nothing new to import.")
        if deferred:
            print(f"{deferred} delivery(ies) still in progress — a later run will import them.")
        return

    num_batches = (len(pending) + batch_size - 1) // batch_size
    total_imported = len(imported_ids)
    imported_this_run = 0
    last_delivery_id: str | None = None

    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        batch_num = i // batch_size + 1
        end = i + len(batch)
        print(
            f"\nBatch {batch_num}/{num_batches} (deliveries {i + 1}–{end} of {len(pending)})...",
            end="  ",
            flush=True,
        )

        imported_in_batch = 0
        for delivery_summary in batch:
            delivery_id = picnic.delivery_id(delivery_summary)

            try:
                detail = await picnic.get_delivery(delivery_id)
            except Exception as exc:
                print(f"\n  Warning: could not fetch delivery {delivery_id}: {exc}")
                deferred += 1
                continue

            # Parsed before opening a session: an unusable detail must leave
            # no row behind, or it would drop out of `pending` for good.
            reason = picnic.delivery_unusable_reason(detail)
            if reason:
                print(f"\n  Warning: deferring delivery {delivery_id}: {reason}")
                deferred += 1
                continue
            fields = picnic.delivery_summary_fields(detail)
            items = picnic.delivery_line_items(detail)

            async with db.get_db() as conn:
                # `pending` already excludes what was in `orders` when this run
                # started; this guards against an overlapping run (e.g. two
                # cron jobs) that imported it since — inserting its items again
                # would double them.  The UNIQUE constraint is the backstop.
                if await db.get_order_id(conn, delivery_id) is not None:
                    continue

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
            last_delivery_id = delivery_id

        total_imported += imported_in_batch
        imported_this_run += imported_in_batch
        print(f"imported {imported_in_batch}", end="")

        # Checkpoint after each batch that imported something
        if imported_in_batch:
            async with db.get_db() as conn:
                await db.save_checkpoint(
                    conn,
                    last_delivery_id=last_delivery_id,
                    total_imported=total_imported,
                )

        if end < len(pending):
            print(f"  [sleeping {delay_seconds}s]", flush=True)
            await asyncio.sleep(delay_seconds)
        else:
            print()

    # Mark as finished, unless something was deferred to the next run
    if imported_this_run:
        async with db.get_db() as conn:
            await db.save_checkpoint(
                conn,
                last_delivery_id=last_delivery_id,
                total_imported=total_imported,
                finished=not deferred,
            )

    print(
        f"\nDone. Imported {imported_this_run} new delivery(ies); "
        f"{total_imported} in the database."
    )
    if deferred:
        print(f"{deferred} delivery(ies) deferred — run the import again later to pick them up.")


if __name__ == "__main__":
    asyncio.run(main())
