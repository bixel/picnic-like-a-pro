"""Thin async-friendly wrapper around python-picnic-api2.

The underlying library is synchronous, so every call is run in a thread pool
executor to avoid blocking the event loop.

Since python-picnic-api2 2.0 the library returns pydantic models.  Everything
here converts them back to plain dicts via ``_plain()`` so callers (MCP tools,
the import script) keep working on JSON-serialisable data.

Set PICNIC_MOCK=true to swap the real Picnic API for the in-process mock
(useful for demos, automated tests, and local development without credentials).
"""

from __future__ import annotations

import asyncio
import os
from functools import partial
from typing import Any


_client = None  # PicnicAPI | MockPicnicAPI | None


def _get_client():
    global _client
    if _client is None:
        if os.getenv("PICNIC_MOCK", "").lower() == "true":
            from .mock_client import get_mock_client  # noqa: PLC0415
            _client = get_mock_client()
        else:
            # Deferred so mock mode never requires the real library to import.
            from python_picnic_api2 import PicnicAPI  # noqa: PLC0415
            _client = PicnicAPI(
                username=os.environ["PICNIC_USERNAME"],
                password=os.environ["PICNIC_PASSWORD"],
                country_code=os.getenv("PICNIC_COUNTRY_CODE", "NL"),
            )
    return _client


def reset_client() -> None:
    """Discard the client singleton so the next call builds a fresh instance."""
    global _client
    _client = None


async def _run(func, *args, **kwargs) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


def _plain(value: Any) -> Any:
    """Turn a python-picnic-api2 model (or a list of them) into a plain dict.

    Models keep the verbatim API payload on ``.raw``, which is exactly what
    1.x returned, so that is preferred over ``model_dump()``.  Plain values
    (as returned by the mock client) pass through unchanged.
    """
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if hasattr(value, "model_dump"):
        raw = getattr(value, "raw", None)
        return raw if isinstance(raw, dict) else value.model_dump(mode="json")
    return value


# ---------------------------------------------------------------------------
# Product search & catalog
# ---------------------------------------------------------------------------

async def search_products(query: str, limit: int = 10) -> list[dict]:
    client = _get_client()
    raw = await _run(client.search, query)
    if hasattr(raw, "items") and not isinstance(raw, (dict, list)):
        # 2.x SearchResult: a flat list of tiles.  Its ``.raw`` is the whole
        # page tree, so dump the parsed tiles instead.
        raw = [{"items": [i.model_dump(mode="json") for i in raw.items]}]
    items: list[dict] = []
    for category in raw:
        for item in category.get("items", []):
            items.append(_normalise_product(item))
            if len(items) >= limit:
                return items
    return items


async def get_categories() -> list[dict]:
    client = _get_client()
    try:
        raw = await _run(client.get_categories)
    except NotImplementedError:
        # Picnic removed this endpoint; python-picnic-api2 2.x raises.
        return []
    return raw if isinstance(raw, list) else []


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------

async def get_cart() -> dict:
    client = _get_client()
    return _plain(await _run(client.get_cart))


async def add_to_cart(product_id: str, quantity: int = 1) -> dict:
    client = _get_client()
    return _plain(await _run(client.add_product, product_id, count=quantity))


async def remove_from_cart(product_id: str) -> dict:
    client = _get_client()
    return _plain(await _run(client.remove_product, product_id))


async def clear_cart() -> dict:
    client = _get_client()
    cart = _plain(await _run(client.get_cart))
    for item in cart.get("items", []):
        pid = item.get("id") or item.get("article_id")
        if pid:
            await _run(client.remove_product, pid)
    return {"status": "cart cleared"}


# ---------------------------------------------------------------------------
# Delivery slots
# ---------------------------------------------------------------------------

async def get_delivery_slots() -> list[dict]:
    client = _get_client()
    raw = _plain(await _run(client.get_delivery_slots))
    if isinstance(raw, dict):
        return raw.get("delivery_slots", [])
    return raw if isinstance(raw, list) else []


# ---------------------------------------------------------------------------
# Order history (from Picnic directly, not local DB)
# ---------------------------------------------------------------------------

async def get_deliveries() -> list[dict]:
    """Return full list of past deliveries (summary only)."""
    client = _get_client()
    return _plain(await _run(client.get_deliveries))


async def get_delivery(delivery_id: str) -> dict:
    """Return detailed delivery data including line items."""
    client = _get_client()
    return _plain(await _run(client.get_delivery, delivery_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_product(raw: dict) -> dict:
    """Map a raw Picnic API product dict to our internal schema.

    Real search tiles carry only ``display_price`` (cents); ``price`` is what
    the mock catalogue uses.
    """
    price = raw.get("display_price") or raw.get("price") or 0
    return {
        "id": raw.get("id", ""),
        "name": raw.get("name", ""),
        "unit_price": int(price) if price else None,
        "unit_quantity": raw.get("unit_quantity") or raw.get("quantity"),
        "image_id": raw.get("image_id"),
        "category": None,  # filled in from search context where available
    }
