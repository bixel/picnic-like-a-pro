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

import json
import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .engine import get_session_factory
from .models import (
    ChatSettings,
    ConversationMessage,
    ImportCheckpoint,
    Order,
    OrderItem,
    Product,
)

logger = logging.getLogger(__name__)


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
    """Return stats for every product that has been ordered at least twice.

    ``avg_interval_days`` must be selected here: the forecasting engine reads
    it straight off each row and treats a missing value as "no interval known",
    silently skipping the product.  The HAVING clause guarantees a count of at
    least 2, so the divisor is never zero.
    """
    stmt = (
        select(
            OrderItem.product_id,
            Product.name,
            func.count().label("order_count"),
            func.avg(OrderItem.quantity).label("avg_quantity"),
            func.max(Order.ordered_at).label("last_ordered_at"),
            func.min(Order.ordered_at).label("first_ordered_at"),
            (
                (
                    func.julianday(func.max(Order.ordered_at))
                    - func.julianday(func.min(Order.ordered_at))
                )
                / (func.count() - 1)
            ).label("avg_interval_days"),
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


# ---------------------------------------------------------------------------
# Conversation history
#
# Messages are grouped into turns (see ConversationMessage in models.py).
# Deletion always operates on whole turns: removing a lone message would leave
# a tool_result with no matching tool_use, which the Anthropic API rejects.
#
# Like the rest of this module, these helpers are commit-neutral — the caller
# owns the transaction.
# ---------------------------------------------------------------------------

async def load_conversation(
    session: AsyncSession, chat_id: int, *, limit: int | None = None
) -> list[dict]:
    """Return stored messages oldest-first as ``{"role", "content"}`` dicts.

    *limit* keeps the most recent N messages (still returned oldest-first).
    """
    stmt = select(ConversationMessage.role, ConversationMessage.content).where(
        ConversationMessage.chat_id == chat_id
    )
    if limit is None:
        stmt = stmt.order_by(ConversationMessage.id)
    else:
        stmt = stmt.order_by(ConversationMessage.id.desc()).limit(limit)

    rows = (await session.execute(stmt)).all()
    messages = _rows_to_messages(rows, chat_id)

    if limit is not None:
        messages.reverse()
    return messages


def _rows_to_messages(rows, chat_id: int) -> list[dict]:
    """Decode ``(role, content)`` rows, skipping any whose JSON will not parse.

    Content is always written with ``json.dumps``, so an unparseable row means
    corruption or manual editing rather than anything a user can cause.

    Skipping keeps the chat *loadable*, but note it does not guarantee the
    result is a valid request: dropping a row from the middle of a turn can
    orphan a ``tool_result``. ``trim_history`` repairs the head only. Fully
    repairing this means dropping the whole turn — see the C2 note in
    PLAN_CONVERSATION_HISTORY.md.
    """
    messages: list[dict] = []
    for row in rows:
        try:
            content = json.loads(row.content)
        except (TypeError, ValueError):
            logger.warning("Skipping unparseable message for chat %d", chat_id)
            continue
        messages.append({"role": row.role, "content": content})
    return messages


async def load_recent_turns(
    session: AsyncSession, chat_id: int, *, max_messages: int
) -> list[dict]:
    """Return the most recent *whole* turns that fit within *max_messages*.

    Loading by message count instead would cut mid-turn whenever the boundary
    fell inside one, handing back a window that opens on an orphaned
    ``tool_result`` — a 400 from the Messages API. Turn ids exist precisely so
    the read path does not have to infer boundaries; this uses them.

    A turn larger than *max_messages* on its own is dropped rather than
    truncated, so the result may be empty.
    """
    if max_messages <= 0:
        return []

    counts = (
        await session.execute(
            select(
                ConversationMessage.turn_id,
                func.count().label("size"),
            )
            .where(ConversationMessage.chat_id == chat_id)
            .group_by(ConversationMessage.turn_id)
            .order_by(func.min(ConversationMessage.id).desc())
        )
    ).all()

    keep: list[int] = []
    total = 0
    for row in counts:                      # newest turn first
        if total + row.size > max_messages:
            break                           # stop at the first turn that overflows
        keep.append(row.turn_id)
        total += row.size

    if not keep:
        return []

    rows = (
        await session.execute(
            select(ConversationMessage.role, ConversationMessage.content)
            .where(
                ConversationMessage.chat_id == chat_id,
                ConversationMessage.turn_id.in_(keep),
            )
            .order_by(ConversationMessage.id)
        )
    ).all()
    return _rows_to_messages(rows, chat_id)


async def append_turn(
    session: AsyncSession, chat_id: int, messages: list[dict]
) -> int:
    """Append one turn's messages under a fresh ``turn_id``. Returns the turn_id.

    Turn ids are per-chat and monotonic. They are not reused after a deletion —
    gaps are expected and harmless.
    """
    next_turn = await session.scalar(
        select(func.coalesce(func.max(ConversationMessage.turn_id), 0) + 1).where(
            ConversationMessage.chat_id == chat_id
        )
    )
    turn_id = int(next_turn or 1)

    now = _now()
    session.add_all(
        [
            ConversationMessage(
                chat_id=chat_id,
                turn_id=turn_id,
                role=m["role"],
                content=json.dumps(m["content"]),
                created_at=now,
            )
            for m in messages
        ]
    )
    return turn_id


async def list_turns(session: AsyncSession, chat_id: int) -> list[dict]:
    """Summarise a chat's turns, oldest first.

    Backs a future "what can I delete?" view; the preview is the opening user
    message of each turn, truncated.
    """
    stmt = (
        select(
            ConversationMessage.turn_id,
            func.min(ConversationMessage.created_at).label("started_at"),
            func.count().label("message_count"),
            func.min(ConversationMessage.id).label("first_id"),
        )
        .where(ConversationMessage.chat_id == chat_id)
        .group_by(ConversationMessage.turn_id)
        .order_by(func.min(ConversationMessage.id))
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []

    first_ids = [row.first_id for row in rows]
    previews_by_id = dict(
        (
            await session.execute(
                select(ConversationMessage.id, ConversationMessage.content).where(
                    ConversationMessage.id.in_(first_ids)
                )
            )
        ).all()
    )

    turns = []
    for row in rows:
        try:
            content = json.loads(previews_by_id.get(row.first_id, '""'))
        except (TypeError, ValueError):
            content = ""
        # Only a plain-text opening message makes a meaningful preview.
        preview = content if isinstance(content, str) else ""
        turns.append(
            {
                "turn_id": row.turn_id,
                "started_at": row.started_at,
                "message_count": row.message_count,
                "preview": preview[:120],
            }
        )
    return turns


async def delete_conversation(session: AsyncSession, chat_id: int) -> int:
    """Delete all stored messages for a chat. Returns the number of rows removed."""
    result = await session.execute(
        delete(ConversationMessage).where(ConversationMessage.chat_id == chat_id)
    )
    return result.rowcount


async def delete_turns(
    session: AsyncSession, chat_id: int, turn_ids: Iterable[int]
) -> int:
    """Delete the named turns in full. Returns the number of rows removed."""
    ids = list(turn_ids)
    if not ids:
        return 0
    result = await session.execute(
        delete(ConversationMessage).where(
            ConversationMessage.chat_id == chat_id,
            ConversationMessage.turn_id.in_(ids),
        )
    )
    return result.rowcount


async def delete_turns_in_range(
    session: AsyncSession,
    chat_id: int,
    *,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """Delete every turn that *started* within ``[since, until)`` (ISO datetimes).

    The range is resolved to a set of whole turns first, rather than deleting
    rows by timestamp — that is what stops a turn straddling the boundary from
    being split in half.
    """
    stmt = (
        select(ConversationMessage.turn_id)
        .where(ConversationMessage.chat_id == chat_id)
        .group_by(ConversationMessage.turn_id)
    )
    if since is not None:
        stmt = stmt.having(func.min(ConversationMessage.created_at) >= since)
    if until is not None:
        stmt = stmt.having(func.min(ConversationMessage.created_at) < until)

    turn_ids = (await session.scalars(stmt)).all()
    return await delete_turns(session, chat_id, turn_ids)


# ---------------------------------------------------------------------------
# Chat settings
# ---------------------------------------------------------------------------

async def get_chat_settings(session: AsyncSession, chat_id: int) -> dict | None:
    row = await session.get(ChatSettings, chat_id)
    if row is None:
        return None
    return {
        "chat_id": row.chat_id,
        "persist_history": row.persist_history,
        "updated_at": row.updated_at,
    }


async def set_chat_persist_history(
    session: AsyncSession, chat_id: int, enabled: bool
) -> None:
    stmt = (
        sqlite_insert(ChatSettings)
        .values(chat_id=chat_id, persist_history=int(enabled), updated_at=_now())
        .on_conflict_do_update(
            index_elements=["chat_id"],
            set_=dict(persist_history=int(enabled), updated_at=_now()),
        )
    )
    await session.execute(stmt)
