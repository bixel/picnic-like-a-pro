"""Log in to Picnic interactively, completing 2FA, and store the session.

Every process (bot, MCP server, import script) starts from the stored session
token, so after this the bot works without a restart and without a new code
until Picnic expires the session. The same login is available in Telegram as
``/picnic_login`` + ``/picnic_code``; this is the version for a shell.

Usage:
    uv run python scripts/picnic_login.py                   # code by SMS
    uv run python scripts/picnic_login.py --channel email   # code by email
    uv run python scripts/picnic_login.py --status          # is a session stored and valid?
    uv run python scripts/picnic_login.py --logout          # forget the stored session

On the server:
    docker compose run --rm picnic-bot python scripts/picnic_login.py

Like ``chat_model.py``, it never creates or alters the schema: it refuses to
run against a database that is missing or not migrated, so a typo in
``DB_PATH`` cannot store the session somewhere the bot never looks.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Ensure the src package is importable when run as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import inspect  # noqa: E402

from picnic_meal_planner.db.engine import get_engine  # noqa: E402
from picnic_meal_planner.picnic import auth  # noqa: E402
from picnic_meal_planner.picnic import client as picnic  # noqa: E402

UPGRADE_HINT = "run `uv run alembic upgrade head`"
MAX_CODE_ATTEMPTS = 3


async def _schema_problem() -> str | None:
    db_path = os.getenv("DB_PATH", "data/picnic.db")
    if not os.path.isfile(db_path):
        return f"no database at DB_PATH={db_path!r} — check the path, or {UPGRADE_HINT}"
    async with get_engine().connect() as conn:
        tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    if "picnic_auth" not in tables:
        return f"no picnic_auth table — {UPGRADE_HINT}"
    return None


async def _confirm() -> int:
    user = await picnic.get_user()
    print(f"Logged in as {user.get('firstname') or '?'}. The session is stored; "
          "the bot picks it up on its next Picnic call.")
    return 0


async def _status() -> int:
    if not await auth.load_token():
        print("No Picnic session stored.")
        return 1
    try:
        return await _confirm()
    except picnic.PicnicLoginRequired as exc:
        print(exc)
        return 1


async def _login(channel: str, read_code=input) -> int:
    from python_picnic_api2 import Picnic2FAError, PicnicAuthError

    try:
        if await picnic.start_login(channel):
            print("No code was needed.")
            return await _confirm()
    except PicnicAuthError:
        print("Picnic rejected PICNIC_USERNAME / PICNIC_PASSWORD.", file=sys.stderr)
        return 1
    except picnic.PicnicLoginRequired as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"Picnic sent a login code by {channel.lower()}.")
    for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
        code = read_code("Code: ").strip()
        try:
            await picnic.submit_login_code(code)
        except Picnic2FAError:
            left = MAX_CODE_ATTEMPTS - attempt
            print(f"Picnic didn't accept that code. {left} attempt(s) left.")
            continue
        return await _confirm()
    print("Giving up. Run this again for a new code.", file=sys.stderr)
    return 1


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--channel", choices=["sms", "email"], default="sms",
                       help="where Picnic sends the code (default: sms)")
    group.add_argument("--status", action="store_true", help="check the stored session")
    group.add_argument("--logout", action="store_true", help="forget the stored session")
    args = parser.parse_args(argv)

    if os.getenv("PICNIC_MOCK", "").lower() == "true":
        print("PICNIC_MOCK=true — there is no real Picnic account to log in to.")
        return 0
    problem = await _schema_problem()
    if problem:
        print(f"Database not usable: {problem}", file=sys.stderr)
        return 1

    if args.status:
        return await _status()
    if args.logout:
        removed = await picnic.logout()
        print("Stored Picnic session removed." if removed else "No session was stored.")
        return 0
    import requests

    try:
        return await _login(args.channel.upper())
    except requests.ConnectionError:
        print("Cannot reach Picnic — check the network and PICNIC_COUNTRY_CODE.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
