"""Database seeder – inserts 25 weekly orders spanning ~6 months.

``seed_database(conn)`` populates products, orders, and order_items with
realistic recurring-shopping data so statistics and forecasting tests always
have sufficient history.

Ordering patterns (relative to ``reference_time``, default now; the most
recent order is 8 days ago):

  Frequency    Orders  Interval  Products
  weekly       25      ~7d       milk, bread, bananas, yogurt, eggs
  biweekly     13      ~14d      coffee, butter, gouda, chicken
  monthly      7       ~28d      spaghetti, rice, olive oil
  occasional   3       ~84d      salmon, chips
  one_time     1       n/a       tomato sauce

Expected forecasting outcomes at test time:
  weekly     → urgency ≈ 1.14  (overdue: 8 days elapsed vs 7-day interval)
  biweekly   → urgency ≈ 0.57  (due in ~6 days)
  monthly    → urgency ≈ 0.29  (due in ~20 days)
  occasional → urgency ≈ 0.10  (due in ~76 days)
  one_time   → urgency = None  (fewer than 2 orders, no interval computable)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from picnic_meal_planner.picnic.mock_client import (
    CATALOG,
    _FREQUENCY_INDICES,
    _ORDER_PATTERNS,
)


def _order_time(reference: datetime, order_index: int) -> str:
    """ISO timestamp for order *order_index* (0 = oldest, 24 = newest)."""
    days_ago = 8 + (24 - order_index) * 7
    return (reference - timedelta(days=days_ago)).isoformat()


async def seed_database(conn, reference_time: datetime | None = None) -> None:
    """Populate *conn* with the full catalog, 25 orders, and their line items.

    Must be called with an open aiosqlite connection; commits before returning.
    """
    from picnic_meal_planner.db.queries import (
        insert_order,
        insert_order_item,
        upsert_product,
    )
    from picnic_meal_planner.db.schema import init_db

    await init_db(conn)

    now = reference_time or datetime.now(timezone.utc)
    price_lookup = {p["id"]: p["price"] for p in CATALOG}

    # Every catalog product is upserted so order_items FK constraints pass.
    for product in CATALOG:
        await upsert_product(conn, {
            "id": product["id"],
            "name": product["name"],
            "unit_price": product["price"],
            "unit_quantity": product["unit_quantity"],
            "image_id": product.get("image_id"),
            "category": product.get("category"),
        })

    for i in range(25):
        ordered_at = _order_time(now, i)
        items = [
            (pid, qty)
            for pid, qty, freq in _ORDER_PATTERNS
            if i in _FREQUENCY_INDICES[freq]
        ]

        order_id = await insert_order(
            conn,
            picnic_order_id=f"mock_delivery_{i + 1:03d}",
            ordered_at=ordered_at,
            delivered_at=(datetime.fromisoformat(ordered_at) + timedelta(days=1)).isoformat(),
            total_price=sum(price_lookup[pid] * qty for pid, qty in items),
        )

        for product_id, quantity in items:
            await insert_order_item(
                conn,
                order_id=order_id,
                product_id=product_id,
                quantity=quantity,
                unit_price=price_lookup[product_id],
            )

    await conn.commit()


def product_ids_with_frequency(frequency: str) -> list[str]:
    """Return the seeded product IDs matching a given ordering frequency."""
    return [pid for pid, _qty, freq in _ORDER_PATTERNS if freq == frequency]
