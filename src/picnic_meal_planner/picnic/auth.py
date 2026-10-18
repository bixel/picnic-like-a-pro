"""Picnic login: the stored session token and the two-factor (2FA) flow.

Picnic may require a one-time code on login, and there is no way to turn
that off.  So the password is used as rarely as possible: a successful login
stores its session token in the database (``picnic_auth``), and every process
— bot, MCP server, import script, preflight — starts from that token.  Only
when no token is stored does the client fall back to a password login.

When a password login needs 2FA, the client does *not* send a code on its
own: an unprompted SMS on every tool call would be worse than an error.  It
raises :class:`PicnicLoginRequired` instead, whose message says how to log in.
Two front ends drive :class:`LoginFlow`:

- ``scripts/picnic_login.py`` — interactive, in a shell
- ``/picnic_login`` + ``/picnic_code`` — in Telegram (``bot.py``)

The library ties the 2FA steps to one ``PicnicAPI`` instance (the code is
verified against the half-authenticated session that requested it), which is
why a pending login is an object held in memory between the two steps.

Failure behaviour follows ``history.py``: reading or writing the token
degrades (log, carry on in memory) because an unwritable DB must not take the
Picnic tools down with it; clearing a token propagates, like every deletion.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from ..db import queries as db

logger = logging.getLogger(__name__)

CHANNELS = ("SMS", "EMAIL")

# How long a requested code stays usable here.  Picnic's own expiry is not
# documented; this only bounds how long a half-finished login is kept.
CODE_TTL_SECONDS = 10 * 60


class PicnicLoginRequired(RuntimeError):
    """The Picnic account has no usable session; a person has to log in.

    The message is written for the end user: MCP tools surface exceptions as
    ``{"error": str(exc)}``, so it is what the model relays in the chat.
    """


class PicnicLoginPending(PicnicLoginRequired):
    """A login is already waiting for its code; starting another would break it."""


HOW_TO_LOGIN = (
    "Send /picnic_login in Telegram, or run "
    "`uv run python scripts/picnic_login.py` on the server."
)


async def _run(func, *args, **kwargs) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


def credentials() -> tuple[str, str]:
    username = os.getenv("PICNIC_USERNAME", "")
    password = os.getenv("PICNIC_PASSWORD", "")
    if not (username and password):
        raise PicnicLoginRequired(
            "No stored Picnic session and PICNIC_USERNAME / PICNIC_PASSWORD "
            "are not set."
        )
    return username, password


def new_api(auth_token: str | None = None):
    """Build a ``PicnicAPI`` without logging in; that is always explicit here."""
    from python_picnic_api2 import PicnicAPI  # noqa: PLC0415

    return PicnicAPI(
        # The library lowercases the code itself, but stray whitespace
        # would end up in the storefront host name.
        country_code=os.getenv("PICNIC_COUNTRY_CODE", "NL").strip(),
        auth_token=auth_token,
    )


def token_of(api) -> str | None:
    """The session's current token; ``None`` for the mock, which has none."""
    session = getattr(api, "session", None)
    return getattr(session, "auth_token", None)


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------

async def load_token() -> str | None:
    try:
        async with db.get_db() as session:
            return await db.get_picnic_token(session)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the stored Picnic token", exc_info=True)
        return None


async def save_token(token: str) -> None:
    try:
        async with db.get_db() as session:
            await db.set_picnic_token(session, token)
            await session.commit()
    except Exception:  # noqa: BLE001
        # The session still works in this process; it just will not survive
        # a restart, which then means a fresh login.
        logger.warning("Could not store the Picnic token", exc_info=True)


async def clear_token(*, only_if: str | None = None) -> int:
    async with db.get_db() as session:
        removed = await db.delete_picnic_token(session, only_if=only_if)
        await session.commit()
    return removed


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------

@dataclass
class _Pending:
    api: Any
    channel: str
    owner: Any = None
    started: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return time.monotonic() - self.started > CODE_TTL_SECONDS


class LoginFlow:
    """Password login, plus the code step when Picnic asks for one.

    ``start()`` returns the logged-in API when no code is needed, else
    ``None`` after asking Picnic to send a code; ``verify()`` then finishes.
    Neither stores the token — the caller installs the API and does that.
    There is one pending login per process: while it waits, ``start()`` from
    anyone but its owner, and every other password login, is refused
    (:meth:`ensure_idle`).
    """

    def __init__(self) -> None:
        self._pending: _Pending | None = None

    @property
    def pending_channel(self) -> str | None:
        if self._pending is None or self._pending.expired():
            return None
        return self._pending.channel

    def cancel(self) -> None:
        self._pending = None

    def ensure_idle(self, owner: Any = None) -> None:
        """Raise :class:`PicnicLoginPending` while someone else's login waits.

        Any new password login would replace the half-authenticated session
        the pending code belongs to, so it is refused until the code arrives
        or the login expires.  The *owner* of the pending login may restart
        it (say, to switch to email); ``owner=None`` never matches.
        """
        pending = self._pending
        if pending is None or pending.expired():
            return
        if owner is not None and owner == pending.owner:
            return
        raise PicnicLoginPending(
            f"A Picnic login is waiting for the code sent by {pending.channel.lower()}. "
            "Send it with /picnic_code, or wait "
            f"{CODE_TTL_SECONDS // 60} minutes for it to expire."
        )

    async def start(self, channel: str = "SMS", *, owner: Any = None):
        from python_picnic_api2 import Picnic2FARequired  # noqa: PLC0415

        channel = channel.upper()
        if channel not in CHANNELS:
            raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
        self.ensure_idle(owner)
        username, password = credentials()
        api = new_api()
        self._pending = None
        try:
            await _run(api.login, username, password)
        except Picnic2FARequired:
            await _run(api.generate_2fa_code, channel)
            self._pending = _Pending(api=api, channel=channel, owner=owner)
            return None
        _require_token(api)
        return api

    async def verify(self, code: str):
        """Finish a pending login.

        A wrong code raises ``Picnic2FAError`` and keeps the login pending, so
        the person can retry with the same code request.
        """
        pending = self._pending
        if pending is None or pending.expired():
            self._pending = None
            raise PicnicLoginRequired(
                "No Picnic login is waiting for a code (or it expired). "
                "Start again with /picnic_login."
            )
        await _run(pending.api.verify_2fa_code, code.strip())
        self._pending = None
        _require_token(pending.api)
        return pending.api


def _require_token(api) -> None:
    from python_picnic_api2 import PicnicAuthError  # noqa: PLC0415

    if not token_of(api):
        raise PicnicAuthError("Picnic login returned no session token")
