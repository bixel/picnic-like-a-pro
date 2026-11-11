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
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .engine import get_session_factory
from .models import (
    ApiRequest,
    Category,
    CategoryParent,
    ChatSettings,
    ConversationMessage,
    ImportCheckpoint,
    Order,
    OrderItem,
    PicnicAuth,
    Product,
    WebLoginToken,
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
    """Insert or update a product row (keyed on ``product["id"]``).

    The category is not touched: only :func:`set_product_category` sets it.
    """
    insert = sqlite_insert(Product)
    stmt = (
        insert
        .values(
            id=product["id"],
            name=product.get("name", ""),
            unit_price=product.get("unit_price"),
            unit_quantity=product.get("unit_quantity"),
            image_id=product.get("image_id"),
            last_seen_at=_now(),
        )
        .on_conflict_do_update(
            index_elements=["id"],
            set_=dict(
                name=product.get("name", ""),
                unit_price=product.get("unit_price"),
                unit_quantity=product.get("unit_quantity"),
                image_id=product.get("image_id"),
                last_seen_at=_now(),
            ),
        )
    )
    await session.execute(stmt)


async def get_product(session: AsyncSession, product_id: str) -> dict | None:
    """The product, with its category's name as ``category`` (``None`` if unknown)."""
    row = (await session.execute(
        select(Product, Category.name)
        .outerjoin(Category, Product.category_id == Category.id)
        .where(Product.id == product_id)
    )).one_or_none()
    if row is None:
        return None
    product, category = row
    return {
        "id": product.id,
        "name": product.name,
        "unit_price": product.unit_price,
        "unit_quantity": product.unit_quantity,
        "image_id": product.image_id,
        "category": category,
        "category_id": product.category_id,
        "last_seen_at": product.last_seen_at,
    }


async def products_without_category(session: AsyncSession) -> list[str]:
    """Ids of the stored products whose category is not known yet."""
    rows = await session.scalars(
        select(Product.id).where(Product.category_id.is_(None)).order_by(Product.id)
    )
    return list(rows)


async def get_category_names(session: AsyncSession) -> dict[int, str]:
    """``{l3_id: name}`` for every stored category."""
    rows = await session.execute(select(Category.id, Category.name))
    return {row.id: row.name for row in rows}


async def get_categories(session: AsyncSession) -> list[dict]:
    """The categories of the stored products, each with its product count.

    Grouped by category id, so two categories with the same name (under
    different L2 parents) stay apart.  ``l1_name`` and ``l2_name`` are ``None``
    until the names have been looked up.
    """
    l1 = aliased(CategoryParent)
    l2 = aliased(CategoryParent)
    stmt = (
        select(
            Category.name,
            Category.id.label("category_id"),
            func.count(Product.id).label("product_count"),
            Category.l1_id,
            l1.name.label("l1_name"),
            Category.l2_id,
            l2.name.label("l2_name"),
        )
        .select_from(Category)
        .join(Product, Product.category_id == Category.id)
        .outerjoin(l1, and_(l1.level == 1, l1.id == Category.l1_id))
        .outerjoin(l2, and_(l2.level == 2, l2.id == Category.l2_id))
        .group_by(Category.id)
        .order_by(Category.name, Category.id)
    )
    return [row._asdict() for row in (await session.execute(stmt)).all()]


async def get_products_in_category(
    session: AsyncSession, category: str | None = None, category_id: int | None = None
) -> list[dict]:
    """The stored products in a category, by id or by exact name.

    *category_id* wins when both are given; a name matches every category of
    that name, as :func:`get_categories` lists it.
    """
    if category_id is not None:
        condition = Category.id == category_id
    elif category is not None:
        condition = Category.name == category
    else:
        raise ValueError("Give a category name or a category_id.")
    stmt = (
        select(Product.id, Product.name, Product.unit_quantity, Product.unit_price)
        .join(Category, Product.category_id == Category.id)
        .where(condition)
        .order_by(Product.name)
    )
    return [row._asdict() for row in (await session.execute(stmt)).all()]


async def upsert_category(
    session: AsyncSession,
    l3_id: int,
    l2_id: int,
    name: str,
    l1_id: int | None = None,
    *,
    checked_at: str | None = None,
) -> bool:
    """Insert or update an L3 category; ``False`` if it was left alone.

    Picnic's answer wins over the stored one: a changed name or L1 id is
    updated (and logged).  *checked_at* is passed when the name comes from a
    page fetched just now, and records that it was confirmed then.

    L3 ids are assumed to be unique across the whole catalogue.  Should a
    stored category turn up under another L2 parent, that assumption is wrong
    and overwriting would merge two categories, so the row is kept and a
    warning logged.  A ``None`` *l1_id* keeps the stored one.
    """
    stored = await session.get(Category, l3_id)
    if stored is None:
        session.add(Category(id=l3_id, l2_id=l2_id, l1_id=l1_id, name=name, checked_at=checked_at))
        await session.flush()
        return True
    if stored.l2_id != l2_id:
        logger.warning(
            "Category %s is stored under L2 %s but Picnic now lists it under L2 %s "
            "(%r); keeping the stored one",
            l3_id, stored.l2_id, l2_id, name,
        )
        return False
    if stored.name != name:
        logger.info("Category %s renamed from %r to %r", l3_id, stored.name, name)
        stored.name = name
    if l1_id is not None and stored.l1_id != l1_id:
        if stored.l1_id is not None:
            logger.info("Category %s moved from L1 %s to L1 %s", l3_id, stored.l1_id, l1_id)
        stored.l1_id = l1_id
    if checked_at is not None:
        stored.checked_at = checked_at
    return True


async def mark_l3s_checked(session: AsyncSession, l2_id: int, checked_at: str) -> None:
    """Record that the L3s of *l2_id* were checked, named on the page or not.

    An L3 its L2 page no longer lists keeps its name, but is not fetched again
    until the next refresh is due.
    """
    await session.execute(
        update(Category).where(Category.l2_id == l2_id).values(checked_at=checked_at)
    )


def _due(checked_at: str | None, before: str | None) -> bool:
    """Whether a row last checked at *checked_at* is due; ``before=None`` = never."""
    return before is not None and (checked_at is None or checked_at < before)


async def l1_ids_due(
    session: AsyncSession, *, retry_before: str, refresh_before: str | None = None
) -> dict[int, list[int]]:
    """``{l1_id: [stored l2_ids]}`` for each L1 whose page should be fetched.

    The page names the L1 and its L2s.  Due when a name was never looked up, when a lookup that found none is
    older than *retry_before*, or when any name is older than *refresh_before*
    (``None``: names are never refreshed).
    """
    pairs = (await session.execute(
        select(Category.l1_id, Category.l2_id).distinct().where(Category.l1_id.is_not(None))
    )).all()
    parents = {
        (row.level, row.id): row
        for row in await session.scalars(select(CategoryParent))
    }
    wanted: dict[int, set[tuple[int, int]]] = {}
    for l1_id, l2_id in pairs:
        wanted.setdefault(l1_id, {(1, l1_id)}).add((2, l2_id))
    due: dict[int, list[int]] = {}
    for l1_id, keys in sorted(wanted.items()):
        for key in keys:
            row = parents.get(key)
            if (
                row is None
                or (row.name is None and _due(row.checked_at, retry_before))
                or _due(row.checked_at, refresh_before)
            ):
                due[l1_id] = sorted(l2 for level, l2 in keys if level == 2)
                break
    return due


async def l2_pages_due(
    session: AsyncSession, *, refresh_before: str | None, limit: int
) -> list[tuple[int, int, int | None]]:
    """``(l2_id, l3_id, l1_id)`` of up to *limit* L2 pages due a refresh of their L3 names.

    Least recently checked first (never checked before all others).  The L3 is
    the one checked longest ago, for the page's URL; the L1 is its stored one,
    for L3s the page names that are not stored yet.
    """
    if refresh_before is None or limit <= 0:
        return []
    rows = (await session.execute(
        select(Category.l2_id, Category.id, Category.l1_id, Category.checked_at)
        .order_by(Category.checked_at.is_not(None), Category.checked_at, Category.id)
    )).all()
    pages: dict[int, tuple[int, int, int | None]] = {}
    for row in rows:
        if row.l2_id not in pages and _due(row.checked_at, refresh_before):
            pages[row.l2_id] = (row.l2_id, row.id, row.l1_id)
    return list(pages.values())[:limit]


async def upsert_category_parent(
    session: AsyncSession,
    level: int,
    parent_id: int,
    name: str | None,
    checked_at: str | None = None,
) -> None:
    """Store the name of the L1 or L2 category *parent_id*.

    A ``None`` *name* records a lookup that found none, and keeps a name
    stored earlier.  A changed name is updated and logged.
    """
    stored = await session.get(CategoryParent, (level, parent_id))
    if stored is None:
        session.add(CategoryParent(level=level, id=parent_id, name=name, checked_at=checked_at))
        await session.flush()
        return
    if name is not None and stored.name != name:
        if stored.name is not None:
            logger.info("L%s category %s renamed from %r to %r", level, parent_id, stored.name, name)
        stored.name = name
    if checked_at is not None:
        stored.checked_at = checked_at


async def set_product_category(session: AsyncSession, product_id: str, category: dict) -> bool:
    """Record *category* (``l1_id``, ``l2_id``, ``l3_id``, ``name``) for a product.

    Upserts the ``categories`` row, then points the product at it.  When
    :func:`upsert_category` refuses the row (its L2 parent differs), the
    product is left without a category and ``False`` returned, so it is never
    filed under another parent's category.
    """
    if not await upsert_category(
        session, category["l3_id"], category["l2_id"], category["name"], category.get("l1_id")
    ):
        return False
    await session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(category_id=category["l3_id"])
    )
    return True


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


async def get_order_id(session: AsyncSession, picnic_order_id: str) -> int | None:
    """Return the local id of the order imported as *picnic_order_id*, if any."""
    return await session.scalar(
        select(Order.id).where(Order.picnic_order_id == picnic_order_id)
    )


async def get_picnic_order_ids(session: AsyncSession) -> set[str]:
    """Return the Picnic delivery id of every order already imported."""
    result = await session.scalars(
        select(Order.picnic_order_id).where(Order.picnic_order_id.is_not(None))
    )
    return set(result.all())


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


async def turn_ids_in_range(
    session: AsyncSession,
    chat_id: int,
    *,
    since: str | None = None,
    until: str | None = None,
) -> list[int]:
    """The turns that *started* within ``[since, until)`` (ISO datetimes).

    The range is resolved to a set of whole turns, rather than deleting rows
    by timestamp — that is what stops a turn straddling the boundary from
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
    return list((await session.scalars(stmt)).all())


async def turn_time_windows(
    session: AsyncSession, chat_id: int, turn_ids: Iterable[int]
) -> list[tuple[str | None, str]]:
    """``(after, until]`` time windows covering each named turn's API requests.

    A turn's messages are all stamped when the turn is *committed*, after
    every request it made, so the turn itself spans no time.  Its requests
    lie between the previous turn's commit (exclusive; ``None`` for the
    chat's first turn) and its own (inclusive).  A turn that was discarded
    rather than committed falls into the next turn's window — deleting more,
    never less.
    """
    wanted = set(turn_ids)
    if not wanted:
        return []
    stmt = (
        select(ConversationMessage.turn_id, func.max(ConversationMessage.created_at))
        .where(ConversationMessage.chat_id == chat_id)
        .group_by(ConversationMessage.turn_id)
        .order_by(func.max(ConversationMessage.created_at))
    )
    windows = []
    previous: str | None = None
    for turn_id, committed_at in (await session.execute(stmt)).all():
        if turn_id in wanted:
            windows.append((previous, committed_at))
        previous = committed_at
    return windows


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
        "model": row.model,
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


async def upsert_chat_model(
    session: AsyncSession, chat_id: int, model: str | None
) -> None:
    """Pin *model* for a chat; ``None`` reverts it to the deployment default.

    Only ``model`` is written on conflict, so the persistence opt-out is never
    touched; a newly created row gets persist_history's server default.
    """
    stmt = (
        sqlite_insert(ChatSettings)
        .values(chat_id=chat_id, model=model, updated_at=_now())
        .on_conflict_do_update(
            index_elements=["chat_id"],
            set_=dict(model=model, updated_at=_now()),
        )
    )
    await session.execute(stmt)


async def list_chat_models(session: AsyncSession) -> list[dict]:
    """Every known chat with its pinned model (``None`` = deployment default).

    A chat is "known" if it has a settings row or any stored message, so chats
    that never touched a setting still show up.
    """
    ids = set(await session.scalars(select(ChatSettings.chat_id)))
    ids |= set(await session.scalars(select(ConversationMessage.chat_id).distinct()))
    pinned = dict((await session.execute(select(ChatSettings.chat_id, ChatSettings.model))).all())
    return [{"chat_id": cid, "model": pinned.get(cid)} for cid in sorted(ids)]


# ---------------------------------------------------------------------------
# Picnic session token
#
# One row (id = 1): the deployment has one Picnic account.  The token is a
# credential, so nothing here logs it.
# ---------------------------------------------------------------------------

_PICNIC_AUTH_ID = 1


async def get_picnic_token(session: AsyncSession) -> str | None:
    return await session.scalar(
        select(PicnicAuth.auth_token).where(PicnicAuth.id == _PICNIC_AUTH_ID)
    )


async def set_picnic_token(session: AsyncSession, token: str) -> None:
    stmt = (
        sqlite_insert(PicnicAuth)
        .values(id=_PICNIC_AUTH_ID, auth_token=token, updated_at=_now())
        .on_conflict_do_update(
            index_elements=["id"],
            set_=dict(auth_token=token, updated_at=_now()),
        )
    )
    await session.execute(stmt)


async def delete_picnic_token(
    session: AsyncSession, *, only_if: str | None = None
) -> int:
    """Forget the stored token; return how many rows were removed (0 or 1).

    With *only_if*, delete only while the stored token is still that one.  A
    process whose token was just rejected must not wipe a fresh token that
    another process (e.g. ``scripts/picnic_login.py``) stored meanwhile.
    """
    stmt = delete(PicnicAuth).where(PicnicAuth.id == _PICNIC_AUTH_ID)
    if only_if is not None:
        stmt = stmt.where(PicnicAuth.auth_token == only_if)
    result = await session.execute(stmt)
    return result.rowcount


# ---------------------------------------------------------------------------
# API request log
#
# Append-only observability data, written best-effort by ``api_log.py``.  A
# failure to write a row must never affect the request being logged, which is
# why ``api_log`` swallows it rather than propagating.  As elsewhere
# in this module, the helpers are commit-neutral: the caller commits.
# ---------------------------------------------------------------------------


def _api_request_dict(row: ApiRequest) -> dict:
    return {
        "id": row.id,
        "provider": row.provider,
        "operation": row.operation,
        "target": row.target,
        "http_method": row.http_method,
        "url": row.url,
        "request_headers": row.request_headers,
        "response_headers": row.response_headers,
        "status": row.status,
        "http_status": row.http_status,
        "duration_ms": row.duration_ms,
        "error_type": row.error_type,
        "error_message": row.error_message,
        "request_body": row.request_body,
        "response_body": row.response_body,
        "source": row.source,
        "run_id": row.run_id,
        "chat_id": row.chat_id,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "requested_at": row.requested_at,
    }


async def insert_api_request(
    session: AsyncSession,
    *,
    provider: str,
    operation: str,
    status: str,
    duration_ms: int,
    requested_at: str,
    target: str | None = None,
    http_method: str | None = None,
    url: str | None = None,
    request_headers: str | None = None,
    response_headers: str | None = None,
    http_status: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    request_body: str | None = None,
    response_body: str | None = None,
    source: str | None = None,
    run_id: str | None = None,
    chat_id: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Append one request-log row; the caller commits."""
    session.add(
        ApiRequest(
            provider=provider,
            operation=operation,
            target=target,
            http_method=http_method,
            url=url,
            request_headers=request_headers,
            response_headers=response_headers,
            status=status,
            http_status=http_status,
            duration_ms=duration_ms,
            error_type=error_type,
            error_message=error_message,
            request_body=request_body,
            response_body=response_body,
            source=source,
            run_id=run_id,
            chat_id=chat_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            requested_at=requested_at,
        )
    )


async def delete_api_requests(
    session: AsyncSession,
    chat_id: int,
    *,
    after: str | None = None,
    until: str | None = None,
) -> int:
    """Delete a chat's logged requests, optionally within ``(after, until]``.

    Returns how many rows were removed; the caller commits.  The bounds match
    :func:`turn_time_windows`.
    """
    stmt = delete(ApiRequest).where(ApiRequest.chat_id == chat_id)
    if after is not None:
        stmt = stmt.where(ApiRequest.requested_at > after)
    if until is not None:
        stmt = stmt.where(ApiRequest.requested_at <= until)
    result = await session.execute(stmt)
    return result.rowcount


async def list_api_requests(
    session: AsyncSession,
    *,
    provider: str | None = None,
    source: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return logged requests, newest first, optionally filtered.

    A convenience for operators and scripts; the bot itself never reads it.
    """
    stmt = select(ApiRequest).order_by(ApiRequest.id.desc()).limit(limit)
    if provider is not None:
        stmt = stmt.where(ApiRequest.provider == provider)
    if source is not None:
        stmt = stmt.where(ApiRequest.source == source)
    if status is not None:
        stmt = stmt.where(ApiRequest.status == status)
    rows = (await session.scalars(stmt)).all()
    return [_api_request_dict(row) for row in rows]


async def prune_api_requests(session: AsyncSession, *, keep_days: int) -> int:
    """Delete log rows older than *keep_days*; return how many were removed.

    The log is otherwise unbounded.  Intended for a periodic operator task
    (cron, or before a backup), not for the request path.
    """
    if keep_days < 0:
        raise ValueError("keep_days must be >= 0")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    result = await session.execute(
        delete(ApiRequest).where(ApiRequest.requested_at < cutoff)
    )
    return result.rowcount


# ---------------------------------------------------------------------------
# Reporting UI: login links
# ---------------------------------------------------------------------------

async def create_login_token(
    session: AsyncSession, token_hash: str, user_id: int, expires_at: str
) -> None:
    """Store a one-time login token (by hash) and drop the expired ones."""
    await session.execute(delete(WebLoginToken).where(WebLoginToken.expires_at <= _now()))
    session.add(WebLoginToken(token_hash=token_hash, user_id=user_id, expires_at=expires_at))


async def consume_login_token(session: AsyncSession, token_hash: str) -> int | None:
    """Mark the token used and return its user id; ``None`` if it cannot be used.

    One UPDATE decides it, so of two requests with the same token only one
    gets the user id.
    """
    now = _now()
    result = await session.execute(
        update(WebLoginToken)
        .where(
            WebLoginToken.token_hash == token_hash,
            WebLoginToken.used_at.is_(None),
            WebLoginToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(WebLoginToken.user_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Reporting UI: raw rows (aggregated by web/reports.py)
# ---------------------------------------------------------------------------

async def report_orders(session: AsyncSession) -> list[dict]:
    """Every order as ``{id, date, total_price}``.

    ``date`` is the delivery time, or the order time when that is unknown.
    """
    stmt = select(
        Order.id,
        func.coalesce(func.nullif(Order.delivered_at, ""), Order.ordered_at).label("date"),
        Order.total_price,
    )
    return [row._asdict() for row in (await session.execute(stmt)).all()]


async def report_order_lines(session: AsyncSession) -> list[dict]:
    """Every order line as ``{order_id, product_id, quantity, unit_price}``."""
    stmt = select(
        OrderItem.order_id, OrderItem.product_id, OrderItem.quantity, OrderItem.unit_price
    )
    return [row._asdict() for row in (await session.execute(stmt)).all()]


async def report_products(session: AsyncSession) -> list[dict]:
    """Every product with its category path; names are ``None`` until looked up."""
    l1 = aliased(CategoryParent)
    l2 = aliased(CategoryParent)
    stmt = (
        select(
            Product.id,
            Product.name,
            Product.unit_quantity,
            Product.category_id.label("l3_id"),
            Category.name.label("l3_name"),
            Category.l2_id,
            l2.name.label("l2_name"),
            Category.l1_id,
            l1.name.label("l1_name"),
        )
        .select_from(Product)
        .outerjoin(Category, Product.category_id == Category.id)
        .outerjoin(l1, and_(l1.level == 1, l1.id == Category.l1_id))
        .outerjoin(l2, and_(l2.level == 2, l2.id == Category.l2_id))
    )
    return [row._asdict() for row in (await session.execute(stmt)).all()]
