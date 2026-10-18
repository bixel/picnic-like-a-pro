"""Thin async-friendly wrapper around python-picnic-api2.

The underlying library is synchronous, so every call is run in a thread pool
executor to avoid blocking the event loop.

Since python-picnic-api2 2.0 the library returns pydantic models.  Everything
here converts them back to plain dicts via ``_plain()`` so callers (MCP tools,
the import script) keep working on JSON-serialisable data.

Logging in is ``auth.py``'s job: the client starts from the session token
stored in the database, and raises :class:`PicnicLoginRequired` when a person
has to log in (2FA).  Every call goes through ``_call()``, which keeps that
stored token current.

Set PICNIC_MOCK=true to swap the real Picnic API for the in-process mock
(useful for demos, automated tests, and local development without credentials).
"""

from __future__ import annotations

import asyncio
import logging
import os
from functools import partial
from typing import Any

from . import auth
from .auth import PicnicLoginRequired

__all__ = ["PicnicLoginRequired"]  # re-exported for callers of this module

logger = logging.getLogger(__name__)

_client = None  # PicnicAPI | MockPicnicAPI | None
_saved_token: str | None = None  # last token written to / read from the DB
# Set once a password login has been answered with a 2FA demand, so later
# calls fail fast instead of repeating the password login on every tool call.
_needs_login = False
_client_lock = asyncio.Lock()
_login = auth.LoginFlow()


def _mocked() -> bool:
    return os.getenv("PICNIC_MOCK", "").lower() == "true"


async def get_client():
    """Return the Picnic client, building it on first use.

    Real mode prefers the stored session token and logs in with the password
    only when none is stored.  Raises :class:`PicnicLoginRequired` when that
    login needs a 2FA code.
    """
    global _client, _saved_token, _needs_login
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client
        if _mocked():
            from .mock_client import get_mock_client  # noqa: PLC0415
            _client = get_mock_client()
            return _client

        token = await auth.load_token()
        if token:
            # A token logged in elsewhere (picnic_login.py, the other process)
            # also clears a pending 2FA demand.
            _client, _saved_token, _needs_login = auth.new_api(token), token, False
            return _client
        if _needs_login:
            raise PicnicLoginRequired(
                f"Picnic needs a one-time login code. {auth.HOW_TO_LOGIN}"
            )

        from python_picnic_api2 import Picnic2FARequired  # noqa: PLC0415

        api = auth.new_api()
        try:
            await _run(api.login, *auth.credentials())
        except Picnic2FARequired:
            _needs_login = True
            raise PicnicLoginRequired(
                f"Picnic needs a one-time login code. {auth.HOW_TO_LOGIN}"
            ) from None
        await _install(api)
        return _client


async def _install(api) -> None:
    """Make *api* the client and store its token for every other process."""
    global _client, _saved_token, _needs_login
    _client, _needs_login = api, False
    token = auth.token_of(api)
    if token:
        await auth.save_token(token)
        _saved_token = token


def reset_client() -> None:
    """Discard the client singleton so the next call builds a fresh instance."""
    global _client, _saved_token, _needs_login, _client_lock
    _client, _saved_token, _needs_login = None, None, False
    # A fresh lock, so one never outlives the event loop it was used on
    # (each test runs on its own loop).
    _client_lock = asyncio.Lock()
    _login.cancel()


async def _run(func, *args, **kwargs) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


async def _call(method: str, *args, **kwargs) -> Any:
    """Call a client method, keeping the stored token in step with the session.

    Picnic may hand out a new token on any response, and the session adopts
    it; storing it keeps a restart from resuming with a stale one.  A
    rejected token is cleared and turned into :class:`PicnicLoginRequired`.
    """
    global _client, _saved_token
    client = await get_client()
    try:
        result = await _run(getattr(client, method), *args, **kwargs)
    except Exception as exc:
        from python_picnic_api2 import PicnicAuthError  # noqa: PLC0415

        if not isinstance(exc, PicnicAuthError) or _mocked():
            raise
        failed = auth.token_of(client)
        logger.warning("Picnic rejected the session token; a new login is needed")
        try:
            await auth.clear_token(only_if=failed)
        except Exception:  # noqa: BLE001
            logger.warning("Could not clear the rejected Picnic token", exc_info=True)
        _client, _saved_token = None, None
        raise PicnicLoginRequired(
            f"The Picnic session has expired. {auth.HOW_TO_LOGIN}"
        ) from exc

    token = auth.token_of(client)
    if token and token != _saved_token:
        await auth.save_token(token)
        _saved_token = token
    return result


# ---------------------------------------------------------------------------
# Login (see auth.py) — shared by /picnic_login and scripts/picnic_login.py
# ---------------------------------------------------------------------------

async def start_login(channel: str = "SMS") -> bool:
    """Log in with the password; ``True`` if done, ``False`` if a code was sent."""
    api = await _login.start(channel)
    if api is None:
        return False
    await _install(api)
    return True


async def submit_login_code(code: str) -> None:
    """Finish a login that is waiting for its 2FA code."""
    await _install(await _login.verify(code))


def pending_login_channel() -> str | None:
    """``"SMS"`` / ``"EMAIL"`` while a login waits for a code, else ``None``."""
    return _login.pending_channel


async def logout() -> int:
    """Forget the session here and in the database."""
    reset_client()
    return await auth.clear_token()


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
    raw = await _call("search", query)
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
    try:
        raw = await _call("get_categories")
    except NotImplementedError:
        # Picnic removed this endpoint; python-picnic-api2 2.x raises.
        return []
    return raw if isinstance(raw, list) else []


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------

async def get_cart() -> dict:
    return _plain(await _call("get_cart"))


async def add_to_cart(product_id: str, quantity: int = 1) -> dict:
    return _plain(await _call("add_product", product_id, count=quantity))


async def remove_from_cart(product_id: str) -> dict:
    return _plain(await _call("remove_product", product_id))


async def clear_cart() -> dict:
    cart = _plain(await _call("get_cart"))
    for item in cart.get("items", []):
        pid = item.get("id") or item.get("article_id")
        if pid:
            await _call("remove_product", pid)
    return {"status": "cart cleared"}


# ---------------------------------------------------------------------------
# Delivery slots
# ---------------------------------------------------------------------------

async def get_delivery_slots() -> list[dict]:
    raw = _plain(await _call("get_delivery_slots"))
    if isinstance(raw, dict):
        return raw.get("delivery_slots", [])
    return raw if isinstance(raw, list) else []


# ---------------------------------------------------------------------------
# Order history (from Picnic directly, not local DB)
# ---------------------------------------------------------------------------

async def get_deliveries() -> list[dict]:
    """Return full list of past deliveries (summary only)."""
    return _plain(await _call("get_deliveries"))


async def get_delivery(delivery_id: str) -> dict:
    """Return detailed delivery data including line items."""
    return _plain(await _call("get_delivery", delivery_id))


async def get_user() -> dict:
    """Return the logged-in account (used by preflight and the login flows)."""
    return _plain(await _call("get_user"))


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


def _price_per_unit(line_price: Any, quantity: int) -> int | None:
    if not isinstance(line_price, int) or quantity <= 0:
        return None
    return round(line_price / quantity)


def _article_quantity(article: dict) -> int:
    """Read the ordered count from an article's ``QUANTITY`` decorator."""
    for decorator in article.get("decorators") or []:
        if decorator.get("type") == "QUANTITY" and decorator.get("quantity"):
            return int(decorator["quantity"])
    return int(article.get("count") or article.get("quantity") or 1)


def delivery_line_items(detail: dict) -> list[dict]:
    """Flatten a delivery's orders into ``{product, quantity, unit_price}`` dicts.

    The real API nests ``orders -> ORDER_LINE -> ORDER_ARTICLE``: the line
    carries the price actually paid for the whole line, the article carries
    the product and, in a ``QUANTITY`` decorator, the count.  The mock skips
    the line level and puts flat articles straight into ``order["items"]``;
    an item without nested ``items`` is treated as that flat article.
    """
    result: list[dict] = []
    for order in detail.get("orders") or [detail]:
        for line in order.get("items") or []:
            nested = line.get("items")
            articles = nested if isinstance(nested, list) else [line]
            for article in articles:
                article = article.get("product") or article
                product_id = article.get("id") or article.get("article_id")
                if not product_id:
                    continue
                quantity = _article_quantity(article)
                # Only a line holding a single article has a line price that
                # belongs to that article alone.
                line_price = line.get("price") if nested and len(articles) == 1 else None
                unit_price = (
                    _price_per_unit(line_price, quantity)
                    or article.get("unit_price")
                    or article.get("price")
                )
                image_ids = article.get("image_ids") or []
                result.append({
                    "product": {
                        "id": product_id,
                        "name": article.get("name", ""),
                        "unit_price": unit_price,
                        "unit_quantity": article.get("unit_quantity"),
                        "image_id": article.get("image_id") or (image_ids[0] if image_ids else None),
                        "category": None,
                    },
                    "quantity": quantity,
                    "unit_price": unit_price,
                })
    return result


def delivery_summary_fields(detail: dict) -> dict:
    """Extract ``ordered_at``, ``delivered_at`` and ``total_price`` from a delivery.

    The real API has no delivery-level total (it is per order) and gives
    ``delivery_time`` as a ``{start, end}`` object when it gives it at all;
    the mock has a flat total and an ISO string.
    """
    slot = detail.get("slot") or {}
    ordered_at = (
        detail.get("creation_time")
        or detail.get("ordered_at")
        or slot.get("window_start")
        or ""
    )

    delivered_at = detail.get("delivery_time") or detail.get("delivered_at")
    if isinstance(delivered_at, dict):
        delivered_at = delivered_at.get("start") or delivered_at.get("end")
    if not isinstance(delivered_at, str):
        delivered_at = None

    total_price = detail.get("total_price") or detail.get("price")
    if total_price is None:
        totals = [
            o.get("total_price")
            for o in detail.get("orders") or []
            if isinstance(o.get("total_price"), int)
        ]
        total_price = sum(totals) if totals else None

    return {"ordered_at": ordered_at, "delivered_at": delivered_at, "total_price": total_price}
