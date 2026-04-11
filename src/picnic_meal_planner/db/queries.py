"""Async query helpers for all database tables.

All public functions accept an :class:`~sqlalchemy.ext.asyncio.AsyncSession`
as their first argument (conventionally named ``session``).  Obtain one via
the :func:`get_db` context manager::

    async with get_db() as session:
        await upsert_product(session, product_dict)
        await session.commit()

Explicit ``await session.commit()`` calls are the caller's responsibility so
that multiple writes can be batched into a single transaction.  The only
exception is :func:`save_checkpoint`, which commits immediately because it is
always the last write in a batch.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .engine import get_session_factory
from .models import ImportCheckpoint, Order, OrderItem, Product


# ---------------------------------------------------------------------------
# Public context manager (re-exported here for backwards compat)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_db():
    """Async context manager that yields an open :class:`AsyncSession`."""
    async with get_session_factory()() as session:
        yield session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

async def upsert_product(session: AsyncSession, product: dict) -> None:
    """Insert or update a product row (keyed on ``product["id"]``)."""
    stmt = (
        sqlite_insert(Product)
        .values(
            id=product["id"],
            name=product.get("name", ""),
            unit_price=product.get("unit_price"),
            unit_quantity=product.get("unit_quantity"),
            image_id=product.get("image_id"),
            category=product.get("category"),
            last_seen_at=_now(),
        )
        .on_conflict_do_update(
            index_elements=["id"],
            set_=dict(
                name=product.get("name", ""),
                unit_price=product.get("unit_price"),
                unit_quantity=product.get("unit_quantity"),
                image_id=product.get("image_id"),
                category=product.get("category"),
                last_seen_at=_now(),
            ),
        )
    )
    await session.execute(stmt)


async def get_product(session: AsyncSession, product_id: str) -> dict | None:
    row = await session.get(Product, product_id)
    if row is None:
        return None
    return {
        "id": row.id,
        "name": row.name,
        "unit_price": row.unit_price,
        "unit_quantity": row.unit_quantity,
        "image_id": row.image_id,
        "category": row.category,
        "last_seen_at": row.last_seen_at,
    }


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def insert_order(
    session: AsyncSession,
    *,
    picnic_order_id: str | None = None,
    ordered_at: str,
    delivered_at: str | None = None,
    total_price: int | None = None,
    notes: str | None = None,
) -> int:
    """Insert an order and return its local integer id.

    If *picnic_order_id* is provided and a row with that value already exists
    (UNIQUE constraint), the existing row's id is returned without modifying
    the row.
    """
    if picnic_order_id is not None:
        stmt = (
            sqlite_insert(Order)
            .values(
                picnic_order_id=picnic_order_id,
                ordered_at=ordered_at,
                delivered_at=delivered_at,
                total_price=total_price,
                notes=notes,
            )
            .on_conflict_do_nothing(index_elements=["picnic_order_id"])
        )
        result = await session.execute(stmt)
        if result.rowcount:
            return result.inserted_primary_key[0]
        # Row already existed — fetch and return the existing id.
        existing_id = await session.scalar(
            select(Order.id).where(Order.picnic_order_id == picnic_order_id)
        )
        return existing_id  # type: ignore[return-value]

    # No picnic_order_id: always insert a new row.
    order = Order(
        ordered_at=ordered_at,
        delivered_at=delivered_at,
        total_price=total_price,
        notes=notes,
    )
    session.add(order)
    await session.flush()  # populate order.id
    return order.id


async def insert_order_item(
    session: AsyncSession,
    *,
    order_id: int,
    product_id: str,
    quantity: int,
    unit_price: int | None = None,
) -> None:
    session.add(
        OrderItem(
            order_id=order_id,
            product_id=product_id,
            quantity=quantity,
            unit_price=unit_price,
        )
    )


async def get_order_history(
    session: AsyncSession, *, limit: int = 20, days_back: int | None = None
) -> list[dict]:
    """Return recent orders with their line items, newest first."""
    stmt = (
        select(
            Order.id,
            Order.picnic_order_id,
            Order.ordered_at,
            Order.delivered_at,
            Order.total_price,
            Order.notes,
            OrderItem.product_id,
            OrderItem.quantity,
            OrderItem.unit_price.label("item_price"),
            Product.name.label("product_name"),
        )
        .outerjoin(OrderItem, OrderItem.order_id == Order.id)
        .outerjoin(Product, Product.id == OrderItem.product_id)
        .order_by(Order.ordered_at.desc())
    )
    if days_back is not None:
        stmt = stmt.where(
            Order.ordered_at >= func.datetime("now", f"-{days_back} days")
        )
    # Over-fetch rows then group in Python (multiple rows per order for items).
    stmt = stmt.limit(limit * 20)

    result = await session.execute(stmt)
    orders: dict[int, dict] = {}
    for row in result.all():
        oid = row.id
        if oid not in orders:
            orders[oid] = {
                "id": oid,
                "picnic_order_id": row.picnic_order_id,
                "ordered_at": row.ordered_at,
                "delivered_at": row.delivered_at,
                "total_price": row.total_price,
                "notes": row.notes,
                "items": [],
            }
        if row.product_id:
            orders[oid]["items"].append(
                {
                    "product_id": row.product_id,
                    "product_name": row.product_name,
                    "quantity": row.quantity,
                    "unit_price": row.item_price,
                }
            )

    return list(orders.values())[:limit]


# ---------------------------------------------------------------------------
# Product stats (for forecasting)
# ---------------------------------------------------------------------------

async def get_product_order_stats(session: AsyncSession, product_id: str) -> dict | None:
    """Return order count, average quantity, and average interval (days) for a product."""
    stmt = (
        select(
            func.count().label("order_count"),
            func.avg(OrderItem.quantity).label("avg_quantity"),
            func.max(Order.ordered_at).label("last_ordered_at"),
            func.min(Order.ordered_at).label("first_ordered_at"),
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.product_id == product_id)
    )
    row = (await session.execute(stmt)).one_or_none()

    if row is None or row.order_count == 0:
        return None

    avg_interval: float | None = None
    if row.order_count >= 2:
        first = datetime.fromisoformat(row.first_ordered_at)
        last = datetime.fromisoformat(row.last_ordered_at)
        span_days = (last - first).total_seconds() / 86400
        avg_interval = span_days / (row.order_count - 1)

    return {
        "product_id": product_id,
        "order_count": row.order_count,
        "avg_quantity": row.avg_quantity,
        "avg_interval_days": avg_interval,
        "last_ordered_at": row.last_ordered_at,
    }


async def get_frequently_ordered(session: AsyncSession, limit: int = 20) -> list[dict]:
    """Return products ranked by order frequency."""
    stmt = (
        select(
            OrderItem.product_id,
            Product.name,
            func.count().label("order_count"),
            func.avg(OrderItem.quantity).label("avg_quantity"),
            func.max(Order.ordered_at).label("last_ordered_at"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .group_by(OrderItem.product_id)
        .order_by(func.count().desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [row._asdict() for row in rows]


async def get_all_product_stats(session: AsyncSession) -> list[dict]:
    """Return stats for every product that has been ordered at least twice."""
    stmt = (
        select(
            OrderItem.product_id,
            Product.name,
            func.count().label("order_count"),
            func.avg(OrderItem.quantity).label("avg_quantity"),
            func.max(Order.ordered_at).label("last_ordered_at"),
            func.min(Order.ordered_at).label("first_ordered_at"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .group_by(OrderItem.product_id)
        .having(func.count() >= 2)
    )
    rows = (await session.execute(stmt)).all()
    return [row._asdict() for row in rows]


# ---------------------------------------------------------------------------
# Import checkpoints
# ---------------------------------------------------------------------------

async def get_latest_checkpoint(session: AsyncSession) -> dict | None:
    row = await session.scalar(
        select(ImportCheckpoint).order_by(ImportCheckpoint.id.desc()).limit(1)
    )
    if row is None:
        return None
    return {
        "id": row.id,
        "last_delivery_id": row.last_delivery_id,
        "imported_at": row.imported_at,
        "total_imported": row.total_imported,
        "finished": row.finished,
    }


async def save_checkpoint(
    session: AsyncSession,
    *,
    last_delivery_id: str | None,
    total_imported: int,
    finished: bool = False,
) -> None:
    session.add(
        ImportCheckpoint(
            last_delivery_id=last_delivery_id,
            imported_at=_now(),
            total_imported=total_imported,
            finished=int(finished),
        )
    )
    await session.commit()
