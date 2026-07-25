"""Async query helpers for all database tables."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .schema import init_db

logger = logging.getLogger(__name__)


def _db_path() -> str:
    return os.getenv("DB_PATH", "data/picnic.db")


@asynccontextmanager
async def get_db():
    """Async context manager that yields an open, initialised aiosqlite connection."""
    async with aiosqlite.connect(_db_path()) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await init_db(conn)
        yield conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

async def upsert_product(conn, product: dict) -> None:
    await conn.execute(
        """
        INSERT INTO products (id, name, unit_price, unit_quantity, image_id, category, last_seen_at)
        VALUES (:id, :name, :unit_price, :unit_quantity, :image_id, :category, :last_seen_at)
        ON CONFLICT(id) DO UPDATE SET
            name          = excluded.name,
            unit_price    = excluded.unit_price,
            unit_quantity = excluded.unit_quantity,
            image_id      = excluded.image_id,
            category      = excluded.category,
            last_seen_at  = excluded.last_seen_at
        """,
        {
            "id": product["id"],
            "name": product.get("name", ""),
            "unit_price": product.get("unit_price"),
            "unit_quantity": product.get("unit_quantity"),
            "image_id": product.get("image_id"),
            "category": product.get("category"),
            "last_seen_at": _now(),
        },
    )


async def get_product(conn, product_id: str) -> dict | None:
    async with conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def insert_order(
    conn,
    *,
    picnic_order_id: str | None = None,
    ordered_at: str,
    delivered_at: str | None = None,
    total_price: int | None = None,
    notes: str | None = None,
) -> int:
    """Insert an order and return its local id."""
    cur = await conn.execute(
        """
        INSERT OR IGNORE INTO orders (picnic_order_id, ordered_at, delivered_at, total_price, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (picnic_order_id, ordered_at, delivered_at, total_price, notes),
    )
    if cur.lastrowid:
        return cur.lastrowid
    # Row already existed (picnic_order_id conflict) — fetch the existing id
    async with conn.execute(
        "SELECT id FROM orders WHERE picnic_order_id = ?", (picnic_order_id,)
    ) as c:
        row = await c.fetchone()
        return row["id"]


async def insert_order_item(
    conn,
    *,
    order_id: int,
    product_id: str,
    quantity: int,
    unit_price: int | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO order_items (order_id, product_id, quantity, unit_price)
        VALUES (?, ?, ?, ?)
        """,
        (order_id, product_id, quantity, unit_price),
    )


async def get_order_history(
    conn, *, limit: int = 20, days_back: int | None = None
) -> list[dict]:
    """Return recent orders with their line items."""
    params: list[Any] = []
    where = ""
    if days_back is not None:
        where = "WHERE o.ordered_at >= datetime('now', ?)"
        params.append(f"-{days_back} days")

    query = f"""
        SELECT
            o.id, o.picnic_order_id, o.ordered_at, o.delivered_at, o.total_price, o.notes,
            oi.product_id, oi.quantity, oi.unit_price as item_price,
            p.name as product_name
        FROM orders o
        LEFT JOIN order_items oi ON oi.order_id = o.id
        LEFT JOIN products p ON p.id = oi.product_id
        {where}
        ORDER BY o.ordered_at DESC
        LIMIT ?
    """
    params.append(limit * 20)  # over-fetch rows, then group in Python

    orders: dict[int, dict] = {}
    async with conn.execute(query, params) as cur:
        async for row in cur:
            oid = row["id"]
            if oid not in orders:
                orders[oid] = {
                    "id": oid,
                    "picnic_order_id": row["picnic_order_id"],
                    "ordered_at": row["ordered_at"],
                    "delivered_at": row["delivered_at"],
                    "total_price": row["total_price"],
                    "notes": row["notes"],
                    "items": [],
                }
            if row["product_id"]:
                orders[oid]["items"].append(
                    {
                        "product_id": row["product_id"],
                        "product_name": row["product_name"],
                        "quantity": row["quantity"],
                        "unit_price": row["item_price"],
                    }
                )

    return list(orders.values())[:limit]


# ---------------------------------------------------------------------------
# Product stats (for forecasting)
# ---------------------------------------------------------------------------

async def get_product_order_stats(conn, product_id: str) -> dict | None:
    """Return order count, average quantity, and average interval (days) for a product."""
    async with conn.execute(
        """
        SELECT
            COUNT(*)          AS order_count,
            AVG(oi.quantity)  AS avg_quantity,
            MAX(o.ordered_at) AS last_ordered_at,
            MIN(o.ordered_at) AS first_ordered_at
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.product_id = ?
        """,
        (product_id,),
    ) as cur:
        row = await cur.fetchone()

    if not row or row["order_count"] == 0:
        return None

    order_count = row["order_count"]
    avg_quantity = row["avg_quantity"]
    last_ordered_at = row["last_ordered_at"]
    first_ordered_at = row["first_ordered_at"]

    avg_interval: float | None = None
    if order_count >= 2:
        first = datetime.fromisoformat(first_ordered_at)
        last = datetime.fromisoformat(last_ordered_at)
        span_days = (last - first).total_seconds() / 86400
        avg_interval = span_days / (order_count - 1)

    return {
        "product_id": product_id,
        "order_count": order_count,
        "avg_quantity": avg_quantity,
        "avg_interval_days": avg_interval,
        "last_ordered_at": last_ordered_at,
    }


async def get_frequently_ordered(conn, limit: int = 20) -> list[dict]:
    """Return products ranked by order frequency."""
    async with conn.execute(
        """
        SELECT
            oi.product_id,
            p.name,
            COUNT(*)         AS order_count,
            AVG(oi.quantity) AS avg_quantity,
            MAX(o.ordered_at) AS last_ordered_at
        FROM order_items oi
        JOIN orders o  ON o.id  = oi.order_id
        JOIN products p ON p.id = oi.product_id
        GROUP BY oi.product_id
        ORDER BY order_count DESC
        LIMIT ?
        """,
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_all_product_stats(conn) -> list[dict]:
    """Return stats for every product that has been ordered at least twice."""
    async with conn.execute(
        """
        SELECT
            oi.product_id,
            p.name,
            COUNT(*)          AS order_count,
            AVG(oi.quantity)  AS avg_quantity,
            MAX(o.ordered_at) AS last_ordered_at,
            MIN(o.ordered_at) AS first_ordered_at
        FROM order_items oi
        JOIN orders o  ON o.id  = oi.order_id
        JOIN products p ON p.id = oi.product_id
        GROUP BY oi.product_id
        HAVING order_count >= 2
        """,
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Import checkpoints
# ---------------------------------------------------------------------------

async def get_latest_checkpoint(conn) -> dict | None:
    async with conn.execute(
        "SELECT * FROM import_checkpoints ORDER BY id DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def save_checkpoint(
    conn,
    *,
    last_delivery_id: str | None,
    total_imported: int,
    finished: bool = False,
) -> None:
    await conn.execute(
        """
        INSERT INTO import_checkpoints (last_delivery_id, imported_at, total_imported, finished)
        VALUES (?, ?, ?, ?)
        """,
        (last_delivery_id, _now(), total_imported, int(finished)),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# Conversation history
#
# Messages are grouped into turns: one turn is everything produced by a single
# exchange, which for a tool-using turn is several messages. Deletion always
# operates on whole turns — removing a lone message would leave a tool_result
# with no matching tool_use, which the Anthropic API rejects.
# ---------------------------------------------------------------------------

async def load_conversation(
    conn, chat_id: int, *, limit: int | None = None
) -> list[dict]:
    """Return stored messages oldest-first as {"role", "content"} dicts.

    `limit` keeps the most recent N messages (still returned oldest-first).
    """
    query = "SELECT role, content FROM conversation_messages WHERE chat_id = ? ORDER BY id"
    params: list[Any] = [chat_id]
    if limit is not None:
        query += " DESC LIMIT ?"
        params.append(limit)

    messages: list[dict] = []
    async with conn.execute(query, params) as cur:
        async for row in cur:
            try:
                content = json.loads(row["content"])
            except (TypeError, ValueError):
                # One unreadable row must not make the whole chat unusable.
                logger.warning("Skipping unparseable message for chat %d", chat_id)
                continue
            messages.append({"role": row["role"], "content": content})

    if limit is not None:
        messages.reverse()
    return messages


async def append_turn(conn, chat_id: int, messages: list[dict]) -> int:
    """Append one turn's messages under a fresh turn_id. Returns the turn_id."""
    async with conn.execute(
        "SELECT COALESCE(MAX(turn_id), 0) + 1 AS next FROM conversation_messages WHERE chat_id = ?",
        (chat_id,),
    ) as cur:
        row = await cur.fetchone()
        turn_id = row["next"]

    now = _now()
    await conn.executemany(
        """
        INSERT INTO conversation_messages (chat_id, turn_id, role, content, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (chat_id, turn_id, m["role"], json.dumps(m["content"]), now)
            for m in messages
        ],
    )
    await conn.commit()
    return turn_id


async def list_turns(conn, chat_id: int) -> list[dict]:
    """Summarise a chat's turns, oldest first, for a future "what can I delete?" view."""
    async with conn.execute(
        """
        SELECT
            turn_id,
            MIN(created_at) AS started_at,
            COUNT(*)        AS message_count,
            MIN(id)         AS first_id
        FROM conversation_messages
        WHERE chat_id = ?
        GROUP BY turn_id
        ORDER BY first_id
        """,
        (chat_id,),
    ) as cur:
        rows = await cur.fetchall()

    turns = []
    for row in rows:
        async with conn.execute(
            "SELECT content FROM conversation_messages WHERE id = ?", (row["first_id"],)
        ) as c:
            first = await c.fetchone()
        try:
            content = json.loads(first["content"]) if first else ""
        except (TypeError, ValueError):
            content = ""
        preview = content if isinstance(content, str) else ""
        turns.append(
            {
                "turn_id": row["turn_id"],
                "started_at": row["started_at"],
                "message_count": row["message_count"],
                "preview": preview[:120],
            }
        )
    return turns


async def delete_conversation(conn, chat_id: int) -> int:
    """Delete all stored messages for a chat. Returns the number of rows removed."""
    cur = await conn.execute(
        "DELETE FROM conversation_messages WHERE chat_id = ?", (chat_id,)
    )
    await conn.commit()
    return cur.rowcount


async def delete_turns(conn, chat_id: int, turn_ids: Iterable[int]) -> int:
    """Delete the named turns in full. Returns the number of rows removed."""
    ids = list(turn_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = await conn.execute(
        f"DELETE FROM conversation_messages WHERE chat_id = ? AND turn_id IN ({placeholders})",
        [chat_id, *ids],
    )
    await conn.commit()
    return cur.rowcount


async def delete_turns_in_range(
    conn, chat_id: int, *, since: str | None = None, until: str | None = None
) -> int:
    """Delete every turn that *started* within [since, until).

    Resolving the range to whole turns first — rather than deleting rows by
    timestamp — is what keeps a turn straddling the boundary from being split.
    """
    conditions = []
    params: list[Any] = [chat_id]
    if since is not None:
        conditions.append("MIN(created_at) >= ?")
        params.append(since)
    if until is not None:
        conditions.append("MIN(created_at) < ?")
        params.append(until)
    having = f"HAVING {' AND '.join(conditions)}" if conditions else ""

    async with conn.execute(
        f"""
        SELECT turn_id FROM conversation_messages
        WHERE chat_id = ?
        GROUP BY turn_id
        {having}
        """,
        params,
    ) as cur:
        rows = await cur.fetchall()

    return await delete_turns(conn, chat_id, [r["turn_id"] for r in rows])


# ---------------------------------------------------------------------------
# Chat settings
# ---------------------------------------------------------------------------

async def get_chat_settings(conn, chat_id: int) -> dict | None:
    async with conn.execute(
        "SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_chat_persist_history(conn, chat_id: int, enabled: bool) -> None:
    await conn.execute(
        """
        INSERT INTO chat_settings (chat_id, persist_history, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            persist_history = excluded.persist_history,
            updated_at      = excluded.updated_at
        """,
        (chat_id, int(enabled), _now()),
    )
    await conn.commit()
