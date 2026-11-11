"""Login for the reporting UI: one-time links and the session cookie.

The bot's ``/report`` command calls :func:`create_login_link`; the web app
redeems the link's token once and answers with a signed session cookie.  The
Telegram allowlist stays the only list of users: the bot checks it before
handing out a link, and the web app checks it again on every request.

Failure behaviour: nothing here degrades.  A database error while creating or
redeeming a token propagates — a login must never succeed unverified.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from ..db import queries as db

logger = logging.getLogger(__name__)

LOGIN_TOKEN_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(days=30)
SESSION_COOKIE = "picnic_session"

_process_secret: bytes | None = None


def base_url() -> str:
    """Where the UI is reachable from a browser (``WEB_BASE_URL``)."""
    return os.getenv("WEB_BASE_URL", "http://localhost:8080").rstrip("/")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_login_link(user_id: int) -> str:
    """A link that logs *user_id* in once, within :data:`LOGIN_TOKEN_TTL`.

    The token sits in the URL fragment, which browsers do not send to the
    server, so it stays out of access logs and referrers; the login page
    posts it.
    """
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + LOGIN_TOKEN_TTL).isoformat()
    async with db.get_db() as session:
        await db.create_login_token(session, _hash(token), user_id, expires_at)
        await session.commit()
    return f"{base_url()}/login#token={token}"


async def redeem_login_token(token: str) -> int | None:
    """The user id a login token belongs to, consuming it; ``None`` if unusable."""
    async with db.get_db() as session:
        user_id = await db.consume_login_token(session, _hash(token))
        await session.commit()
    return user_id


def _secret() -> bytes:
    """The cookie signing key: ``WEB_SESSION_SECRET``, else one per process."""
    global _process_secret
    configured = os.getenv("WEB_SESSION_SECRET", "").strip()
    if configured:
        return configured.encode()
    if _process_secret is None:
        logger.warning(
            "WEB_SESSION_SECRET is not set: sessions end when this process restarts."
        )
        _process_secret = secrets.token_bytes(32)
    return _process_secret


def _signature(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def sign_session(user_id: int, now: float | None = None) -> str:
    """The cookie value for a session of *user_id* lasting :data:`SESSION_TTL`."""
    expires = int((now if now is not None else time.time()) + SESSION_TTL.total_seconds())
    payload = f"{user_id}.{expires}"
    return f"{payload}.{_signature(payload)}"


def verify_session(value: str | None, now: float | None = None) -> int | None:
    """The user id of a valid, unexpired session cookie, else ``None``."""
    if not value:
        return None
    payload, _, signature = value.rpartition(".")
    if not payload or not hmac.compare_digest(signature, _signature(payload)):
        return None
    try:
        user_id, expires = (int(part) for part in payload.split("."))
    except ValueError:
        return None
    if expires <= (now if now is not None else time.time()):
        return None
    return user_id
