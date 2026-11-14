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
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from .. import api_log
from . import auth
from .auth import PicnicLoginPending, PicnicLoginRequired


class PicnicRateLimited(RuntimeError):
    """Picnic is refusing requests for now: a 429, or a 403 with an HTML body.

    Every further request would fail the same way until the limit lifts, so a
    caller making many requests should wait and retry, or stop.
    ``retry_after`` is Picnic's ``Retry-After`` in seconds, when it sent one.
    """

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        self.status = status
        self.retry_after = retry_after
        wait = f"; retry after {retry_after:.0f} s" if retry_after is not None else ""
        reason = "rate limited" if status == 429 else "blocked"
        super().__init__(f"Picnic {reason} the request (HTTP {status}{wait}). Try again later.")


__all__ = ["PicnicLoginPending", "PicnicLoginRequired"]  # re-exported for callers of this module

logger = logging.getLogger(__name__)

# A bulk run stops on a login demand, or once the rate-limit backoff gives up.
STOP_ERRORS = (PicnicLoginRequired, PicnicLoginPending, PicnicRateLimited)

# A Retry-After beyond this ends a bulk run's request loop instead of waiting.
MAX_BACKOFF_SECONDS = 15 * 60

_sleep = asyncio.sleep  # replaced by the tests


async def with_backoff[T](func: Callable[..., Awaitable[T]], *args) -> T:
    """``await func(*args)``, waiting and retrying while Picnic rate limits.

    Raises :class:`PicnicRateLimited` once the retries are used up, or when
    Picnic asks for a wait longer than :data:`MAX_BACKOFF_SECONDS`.  The
    many-request scripts (``import_history``, ``repair_order_prices``) share
    this one policy.
    """
    retries = int(os.getenv("IMPORT_BACKOFF_RETRIES", "4"))
    base = float(os.getenv("IMPORT_BACKOFF_SECONDS", "60"))
    for attempt in range(retries + 1):
        try:
            return await func(*args)
        except PicnicRateLimited as exc:
            wait = exc.retry_after if exc.retry_after is not None else base * 2**attempt
            if attempt == retries or wait > MAX_BACKOFF_SECONDS:
                raise
            print(f"  {exc} Waiting {wait:.0f} s (retry {attempt + 1}/{retries})...", flush=True)
            await _sleep(wait)
    raise AssertionError("unreachable")

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
    login needs a 2FA code, and :class:`PicnicLoginPending` instead of
    attempting it while a ``/picnic_login`` waits for its code.
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
        # A password login here would replace the session a pending
        # /picnic_login code belongs to.
        _login.ensure_idle()
        if _needs_login:
            raise PicnicLoginRequired(
                f"Picnic needs a one-time login code. {auth.HOW_TO_LOGIN}"
            )

        from python_picnic_api2 import Picnic2FARequired  # noqa: PLC0415

        username, password = auth.credentials()
        api = auth.new_api()
        try:
            # Logged like every other login step; see auth.login_step().
            await auth.login_step(api.login, username, password)
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
    """Run the blocking Picnic library in a worker thread, without logging.

    The caller logs: :func:`_call` wraps it in a capture block, and login steps
    go through ``auth.login_step()`` instead.

    ``asyncio.to_thread`` (unlike ``run_in_executor``) copies the current
    context, which is how the HTTP capture list reaches the session wrapper.
    """
    return await asyncio.to_thread(func, *args, **kwargs)


# Methods whose first argument identifies the resource, and so is useful as
# the indexed ``target``.  The argument is also visible in the request body;
# this only decides what is easy to filter on.
_LOGGED_TARGET_METHODS = frozenset(
    {"get_article", "get_delivery", "add_product", "remove_product"}
)


def _log_target(method: str, args: tuple) -> str | None:
    if args and method in _LOGGED_TARGET_METHODS:
        return str(args[0])
    return None


def rate_limit_of(captures) -> PicnicRateLimited | None:
    """The rate limit or block shown by the last HTTP response, if any.

    Picnic signals a rate limit with 429, and blocks with a 403 whose body is
    an HTML page.  A 403 with a JSON body is an authentication error (handled
    by the library), and any other status (a 404 for a product no longer
    listed, a 5xx) concerns that one request only.
    """
    answered = [c for c in captures if getattr(c, "status_code", None) is not None]
    if not answered:
        return None
    last = answered[-1]
    if last.status_code == 429 or (last.status_code == 403 and not _is_json(last.response_body)):
        return PicnicRateLimited(last.status_code, _retry_after(last.response_headers))
    return None


def _is_json(body: Any) -> bool:
    if not isinstance(body, str) or not body.strip():
        return False
    try:
        json.loads(body)
    except ValueError:
        return False
    return True


def _retry_after(headers: dict | None) -> float | None:
    """``Retry-After`` in seconds: either delay-seconds or an HTTP date."""
    value = next(
        (v for k, v in (headers or {}).items() if k.lower() == "retry-after"), None
    )
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


async def _call(method: str, *args, **kwargs) -> Any:
    """Call a client method, keeping the stored token in step with the session.

    Picnic may hand out a new token on any response, and the session adopts
    it; storing it keeps a restart from resuming with a stale one.  A
    rejected token is cleared and turned into :class:`PicnicLoginRequired`.

    The call is logged to ``api_requests`` (best-effort) at this single choke
    point, so every caller gets it.  The session wrapper captures the actual
    HTTP method, URL, headers and bodies; the fallback covers the mock client
    and failures that happen before a request is sent.
    """
    global _client, _saved_token
    client = await get_client()
    session = None
    try:
        async with api_log.capture_http_requests(
            api_log.PROVIDER_PICNIC,
            method,
            target=_log_target(method, args),
            request_body=api_log.call_body(*args, **kwargs),
        ) as session:
            session.response_body = await _run(
                getattr(client, method), *args, **kwargs
            )
            result = session.response_body
            # The library returns a JSON 429 body as if it were the answer.
            limited = rate_limit_of(session.captures)
            if limited is not None:
                raise limited
    except Exception as exc:
        from python_picnic_api2 import PicnicAuthError  # noqa: PLC0415

        if not isinstance(exc, PicnicRateLimited):
            # An HTML block makes the library fail on .json(); say what it was.
            limited = rate_limit_of(session.captures) if session is not None else None
            if limited is not None:
                raise limited from exc
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

async def start_login(channel: str = "SMS", *, owner: Any = None) -> bool:
    """Log in with the password; ``True`` if done, ``False`` if a code was sent.

    Raises :class:`PicnicLoginPending` while a login started by someone other
    than *owner* waits for its code.
    """
    api = await _login.start(channel, owner=owner)
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


def _category_ids(article) -> tuple[int | None, int, int] | None:
    """``(l1_id, l2_id, l3_id)`` from a product page, or ``None``.

    The page's category button links to
    ``app.picnic://categories/{l1}/l2/{l2}/l3/{l3}``.
    """
    if isinstance(article, dict):  # the mock client
        ids = article.get("category_ids")
        return tuple(ids) if ids else None
    from python_picnic_api2.models import Article  # noqa: PLC0415

    raw = getattr(article, "raw", None)
    return Article.category_ids_from_page(raw) if isinstance(raw, dict) else None


_L3_HEADER_PREFIX = "vertical-article-tiles-sub-header-"

# The library's own L2 page path (``PicnicAPI.get_category_by_ids``).  That
# method raises when the requested L3 is missing from the page and throws the
# page away, so the page is fetched here directly and every L3 on it is kept.
L2_PAGE_PATH = "/pages/L2-category-page-root?category_id={l2_id}&l3_category_id={l3_id}"
L1_PAGE_PATH = "/pages/L1-category-page-root?category_id={l1_id}"
_L2_ITEM_PREFIX = "core-list-item-category-"


def l3_names_on_page(page) -> dict[int, str]:
    """``{l3_id: name}`` for every L3 sub-header on an L2 category page.

    The page lists its L3 categories under headers with the id
    ``vertical-article-tiles-sub-header-{l3_id}``, labelled with the name.
    """
    if not isinstance(page, dict):
        return {}
    from python_picnic_api2.models import pml  # noqa: PLC0415

    names: dict[int, str] = {}
    for node in pml.find_all(page, id_prefix=_L3_HEADER_PREFIX):
        suffix = node["id"][len(_L3_HEADER_PREFIX):]
        label = pml.accessibility_label(node)
        if suffix.isdigit() and label:
            names[int(suffix)] = label
    return names


def l1_names_on_page(page) -> tuple[str | None, dict[int, str]]:
    """``(l1_name, {l2_id: name})`` from an L1 category page.

    The page's title is the L1 name, and it lists its L2 categories as items
    with the id ``core-list-item-category-{l2_id}``, labelled with the name.
    Picnic's curated entries (``...-CustomCat...``) are not real L2s: skipped.
    """
    if not isinstance(page, dict):
        return None, {}
    from python_picnic_api2.models import pml  # noqa: PLC0415

    header = page.get("header")
    title = header.get("title") if isinstance(header, dict) else None
    names: dict[int, str] = {}
    for node in pml.find_all(page, id_prefix=_L2_ITEM_PREFIX):
        suffix = node["id"][len(_L2_ITEM_PREFIX):]
        label = pml.accessibility_label(node)
        if suffix.isdigit() and label:
            names[int(suffix)] = label
    return (title.strip() or None) if isinstance(title, str) else None, names


async def get_category_ids(product_id: str) -> tuple[int | None, int, int] | None:
    """A product's ``(l1_id, l2_id, l3_id)``, read from its product page.

    One request.  ``None`` means Picnic has no page for the product any more,
    or the page has no category button.  Login errors propagate.
    """
    from python_picnic_api2 import PicnicParseError  # noqa: PLC0415

    try:
        article = await _call("get_article", product_id)
    except PicnicParseError:
        return None
    return None if article is None else _category_ids(article)


async def get_article_price(product_id: str) -> int | None:
    """The current catalogue price (cents) of a product, from its product page.

    One request.  Used by the repair script to backfill ``products.unit_price``.
    ``None`` when Picnic has no page for the product any more, or the page has
    no price.  The value is the *current* price — possibly an on-sale price —
    which matches what ``search_products`` stores as ``unit_price``.  Bundles
    and multipacks are skipped (``None``): their page exposes only a per-unit
    price, which would not match the pack price search stores.  Login errors
    propagate.
    """
    article = await _call("get_article", product_id)
    if article is None:
        return None
    if isinstance(article, dict):  # the mock client
        price = article.get("price")
        return int(price) if price else None
    if getattr(article, "is_bundle", False):
        # Bundle/multipack pages expose only a per-unit price; search stores the
        # pack price, so skip rather than write a per-unit value under a pack's
        # product id.
        return None
    price = getattr(article, "price", None)
    return price if isinstance(price, int) else None


async def get_l3_names(l2_id: int, l3_id: int) -> dict[int, str]:
    """``{l3_id: name}`` for every L3 category on an L2 category page.

    One request.  *l3_id* is only part of the page's URL; the result is
    whatever the page lists, which may or may not include it.  Login errors
    propagate.
    """
    page = await _call("_get", L2_PAGE_PATH.format(l2_id=l2_id, l3_id=l3_id), add_picnic_headers=True)
    return l3_names_on_page(page)


async def get_l1_names(l1_id: int) -> tuple[str | None, dict[int, str]]:
    """``(l1_name, {l2_id: name})`` for an L1 category, in one request."""
    page = await _call("_get", L1_PAGE_PATH.format(l1_id=l1_id), add_picnic_headers=True)
    return l1_names_on_page(page)


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
    }


def _price_per_unit(line_price: Any, quantity: int) -> int | None:
    if not isinstance(line_price, int) or quantity <= 0:
        return None
    # A per-unit price is an integer, so a line total not divisible by the
    # quantity is reproduced to within half a unit (199 over 2 stores 100, a
    # 1-cent over-count).  The exact paid total is not stored separately, so
    # this is the closest the schema allows.
    return round(line_price / quantity)


#: The sentinel Picnic used to put in ``ORDER_ARTICLE.price`` for mixed-promo
#: lines (4321.99 €).  It is not a real price and must never be stored.
PLACEHOLDER_PRICE = 432199


def _article_price_or_none(entry: dict, article: dict) -> int | None:
    """The article's own price, or ``None`` when it is the placeholder.

    Only used when a nested line carries no line-level paid price.  A real
    article price is kept; the 432199 placeholder from historical payloads is
    rejected so it is never stored as spend.
    """
    price = entry.get("unit_price") or article.get("unit_price") or article.get("price")
    return price if isinstance(price, int) and price != PLACEHOLDER_PRICE else None


def _line_paid_price(line: dict) -> int | None:
    """The price actually paid for a delivery line, in cents.

    A ``PRICE`` decorator carries the paid price (``display_price``) and is
    present only when a promotion applied; otherwise the line's own ``price``
    is the paid price.  ``price`` is the pre-promotion price when the decorator
    is present, so it must never be read in that case.
    """
    for decorator in line.get("decorators") or []:
        if decorator.get("type") == "PRICE" and isinstance(
            decorator.get("display_price"), int
        ):
            return decorator["display_price"]
    price = line.get("price")
    return price if isinstance(price, int) else None


def _split_price(total: int, quantities: list[int]) -> list[int]:
    """Split ``total`` cents across articles in proportion to ``quantities``.

    Returns one per-unit price per article.  ``sum(p * q for p, q in
    zip(result, quantities)) == total`` whenever the remainder can be formed
    from the quantities — in particular for a mix-and-match line ("3 für
    5.99€"), where every article has quantity 1 and the split is always exact.

    For mixed quantities the split can be lossy: a +1 cent goes to a per-*unit*
    price, so it moves the line total by that article's quantity, and a
    remainder no combination of quantities reaches cannot be represented at all
    (e.g. ``[5, 5]`` can only hit multiples of 5).  The greedy in-order fill can
    also leave a remainder it could have avoided.  The unassigned leftover is
    therefore up to the smallest quantity minus one in the uniform case, and a
    little more for mixed quantities — several cents, not one or two — which is
    acceptable for a split Picnic does not specify.
    """
    if not quantities:
        return []
    total_units = sum(quantities)
    if total_units <= 0:
        return [0] * len(quantities)
    base, remainder = divmod(total, total_units)
    prices = [base] * len(quantities)
    for index, quantity in enumerate(quantities):
        if quantity <= remainder:
            prices[index] += 1
            remainder -= quantity
        if remainder <= 0:
            break
    return prices


def _article_quantity(article: dict) -> int | None:
    """Read the ordered count from a ``QUANTITY`` decorator or a count field."""
    for decorator in article.get("decorators") or []:
        if decorator.get("type") == "QUANTITY" and decorator.get("quantity"):
            return int(decorator["quantity"])
    count = article.get("count") or article.get("quantity")
    return int(count) if count else None


def delivery_id(summary: dict) -> str | None:
    """The id a delivery summary is keyed by in checkpoints and ``orders``.

    The precedence is load-bearing: changing it would re-key existing imports.
    python-picnic-api2 2.x summaries carry only ``delivery_id``; the mock and
    full delivery payloads have ``id``.
    """
    return summary.get("id") or summary.get("delivery_id") or summary.get("order_id")


def delivery_line_items(detail: dict) -> list[dict]:
    """Flatten a delivery's orders into ``{product, quantity, unit_price}`` dicts.

    The real API nests ``orders -> ORDER_LINE -> ORDER_ARTICLE``.  A line
    carries the price for the whole line — ``price`` is the pre-promotion price
    and, when a promotion applied, a ``PRICE`` decorator's ``display_price`` is
    what was actually paid (see :func:`_line_paid_price`).  Each article carries
    the product and, in a ``QUANTITY`` decorator, the count.  A line holding
    several articles is a mix-and-match promotion whose paid price is split
    across them.  A line with no paid price at all falls back to each article's
    own price, unless that price is the 432199 placeholder.

    ``unit_price`` is the paid price per unit and feeds ``order_items`` (spend).
    The product dict deliberately carries no ``unit_price``: order data is not a
    catalogue price, and ``products.unit_price`` is populated from search and
    the repair backfill instead.

    The mock skips the line level and puts flat articles straight into
    ``order["items"]``; an item without nested ``items`` is treated as that flat
    article, and its own price fields are kept.  Such an item may also wrap the
    article as ``{"product": {...}, "count", "unit_price"}``, in which case the
    wrapper's count and paid price win over the product's.
    """
    result: list[dict] = []
    for order in detail.get("orders") or [detail]:
        for line in order.get("items") or []:
            nested = line.get("items")
            entries: list[tuple[dict, dict, str, int]] = []
            for entry in (nested if isinstance(nested, list) else [line]):
                article = entry.get("product") or entry
                product_id = article.get("id") or article.get("article_id")
                if not product_id:
                    continue
                quantity = _article_quantity(entry) or _article_quantity(article) or 1
                entries.append((entry, article, product_id, quantity))

            if not entries:
                continue

            if isinstance(nested, list):
                # Real payload: the paid price comes from the line (``PRICE``
                # decorator, else the line's own price).  When the line carries
                # none, fall back to the article's own price — but never the
                # 432199 placeholder.
                paid = _line_paid_price(line)
                if isinstance(paid, int):
                    if len(entries) == 1:
                        unit_prices = [_price_per_unit(paid, entries[0][3])]
                    else:
                        unit_prices = _split_price(paid, [q for _, _, _, q in entries])
                else:
                    unit_prices = [
                        _article_price_or_none(entry, article)
                        for entry, article, _, _ in entries
                    ]
            else:
                # Flat mock / legacy wrapper: keep the article's own price.
                unit_prices = [
                    entry.get("unit_price")
                    or article.get("unit_price")
                    or article.get("price")
                    for entry, article, _, _ in entries
                ]

            for (_, article, product_id, quantity), unit_price in zip(entries, unit_prices):
                image_ids = article.get("image_ids") or []
                result.append({
                    "product": {
                        "id": product_id,
                        "name": article.get("name", ""),
                        "unit_quantity": article.get("unit_quantity"),
                        "image_id": article.get("image_id") or (image_ids[0] if image_ids else None),
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


def delivery_unusable_reason(detail: dict) -> str | None:
    """Say why a delivery detail must not be imported, or ``None`` if it may.

    ``PicnicAPI`` only raises on auth errors, so an error body or an empty
    reply arrives as an ordinary dict.  Importing one would store an order
    with no items that the importer then treats as done, so it is never
    retried.  ``error`` is checked for truthiness: real payloads carry
    ``"error": {}``.
    """
    if not isinstance(detail, dict) or not detail:
        return "empty response"
    if detail.get("error"):
        return f"error response: {str(detail['error'])[:80]}"
    if not delivery_summary_fields(detail)["ordered_at"]:
        return "no order time"
    if not delivery_line_items(detail):
        return "no order lines"
    return None
