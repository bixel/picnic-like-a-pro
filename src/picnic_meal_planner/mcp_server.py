"""FastMCP server exposing Picnic, DB, and forecasting tools.

Transport is selected via MCP_TRANSPORT env var:
  stdio  (default) — for Claude Desktop
  sse              — for remote / multi-client access
  http             — streamable-HTTP
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

from .db import queries as db
from .picnic import client as picnic
from .forecasting import engine as forecasting

mcp = FastMCP(
    "picnic-meal-planner",
    instructions=(
        "You are a family grocery and meal planning assistant. "
        "Use the available tools to search for products, manage the Picnic cart, "
        "view order history, and forecast what the family needs to reorder."
    ),
)


# ---------------------------------------------------------------------------
# Picnic API tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_products(query: str, limit: int = 10) -> list[dict]:
    """Search the Picnic product catalog.

    Args:
        query: Search term, e.g. "milk" or "pasta sauce".
        limit: Maximum number of results to return (default 10).
    """
    results = await picnic.search_products(query, limit=limit)
    async with db.get_db() as conn:
        for product in results:
            await db.upsert_product(conn, product)
        await conn.commit()
    return results


@mcp.tool()
async def get_categories() -> list[dict]:
    """Browse the top-level Picnic product categories."""
    return await picnic.get_categories()


@mcp.tool()
async def get_cart() -> dict:
    """Return the current Picnic cart contents."""
    return await picnic.get_cart()


@mcp.tool()
async def add_to_cart(product_id: str, quantity: int = 1) -> dict:
    """Add a product to the Picnic cart.

    Args:
        product_id: The Picnic product/article ID.
        quantity: Number of units to add (default 1).
    """
    return await picnic.add_to_cart(product_id, quantity)


@mcp.tool()
async def remove_from_cart(product_id: str) -> dict:
    """Remove a product from the Picnic cart.

    Args:
        product_id: The Picnic product/article ID to remove.
    """
    return await picnic.remove_from_cart(product_id)


@mcp.tool()
async def clear_cart() -> dict:
    """Empty the entire Picnic cart."""
    return await picnic.clear_cart()


@mcp.tool()
async def get_delivery_slots() -> list[dict]:
    """List available Picnic delivery windows."""
    return await picnic.get_delivery_slots()


# ---------------------------------------------------------------------------
# Order history & DB tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_order_history(limit: int = 10, days_back: int | None = None) -> list[dict]:
    """Return recent orders with their line items from the local database.

    Args:
        limit: Maximum number of orders to return (default 10).
        days_back: If set, only return orders from the last N days.
    """
    async with db.get_db() as conn:
        return await db.get_order_history(conn, limit=limit, days_back=days_back)


@mcp.tool()
async def get_product_order_stats(product_id: str) -> dict | None:
    """Return order statistics for a single product.

    Includes order count, average quantity, average interval between orders,
    and the date of the most recent order.

    Args:
        product_id: The Picnic product/article ID.
    """
    async with db.get_db() as conn:
        return await db.get_product_order_stats(conn, product_id)


@mcp.tool()
async def get_frequently_ordered(limit: int = 20) -> list[dict]:
    """Return products ranked by order frequency from the local database.

    Args:
        limit: Maximum number of products to return (default 20).
    """
    async with db.get_db() as conn:
        return await db.get_frequently_ordered(conn, limit=limit)


@mcp.tool()
async def record_order(
    items: list[dict],
    picnic_order_id: str | None = None,
    notes: str | None = None,
) -> dict:
    """Persist a new order to the local database after checkout.

    Args:
        items: List of {product_id, quantity, unit_price} dicts.
        picnic_order_id: Optional Picnic delivery ID to link to the live order.
        notes: Optional free text, e.g. "birthday party week".
    """
    ordered_at = datetime.now(timezone.utc).isoformat()
    total_price = sum(
        (item.get("unit_price") or 0) * item.get("quantity", 1)
        for item in items
    )
    async with db.get_db() as conn:
        order_id = await db.insert_order(
            conn,
            picnic_order_id=picnic_order_id,
            ordered_at=ordered_at,
            total_price=total_price,
            notes=notes,
        )
        for item in items:
            await db.insert_order_item(
                conn,
                order_id=order_id,
                product_id=item["product_id"],
                quantity=item.get("quantity", 1),
                unit_price=item.get("unit_price"),
            )
        await conn.commit()
    return {"order_id": order_id, "ordered_at": ordered_at, "item_count": len(items)}


# ---------------------------------------------------------------------------
# Forecasting tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def forecast_order(horizon_days: int = 7) -> list[dict]:
    """Predict which products are likely to run out within the next N days.

    Products are ranked by urgency (most overdue first).

    Args:
        horizon_days: Planning window in days (default 7).
    """
    async with db.get_db() as conn:
        all_stats = await db.get_all_product_stats(conn)
    return forecasting.forecast_products(all_stats, horizon_days=horizon_days)


@mcp.tool()
async def get_reorder_due() -> list[dict]:
    """Return products whose reorder interval has already elapsed (urgency score >= 1.0).

    These are items the family is statistically overdue to reorder, sorted by
    how overdue they are.
    """
    async with db.get_db() as conn:
        all_stats = await db.get_all_product_stats(conn)
    return forecasting.get_reorder_due(all_stats)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport in ("sse", "http"):
        host = os.getenv("MCP_HOST", "127.0.0.1")
        port = int(os.getenv("MCP_PORT", "8000"))
        mcp.run(transport=transport, host=host, port=port)
    else:
        raise ValueError(f"Unknown MCP_TRANSPORT: {transport!r}. Use stdio, sse, or http.")


if __name__ == "__main__":
    main()
