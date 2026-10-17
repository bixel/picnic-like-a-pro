"""Check every prerequisite for running the bot locally, before starting it.

Each misconfiguration otherwise surfaces one at a time, mid-walkthrough: the
bot starts, you send ``/plan``, and only then does an invalid API key turn up
as a 401. This checks the lot up front and says which one is wrong.

Usage:
    uv run python scripts/preflight.py          # config + live API checks
    uv run python scripts/preflight.py --offline  # skip network calls

Secrets are never printed — only their length, first/last characters, and
whether they look malformed. The Telegram bot's @username *is* printed, so you
can confirm you are about to run your test bot rather than the production one.
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import dotenv_values, find_dotenv, load_dotenv

# Placeholders shipped in this repo's env templates. Copying one into .env and
# forgetting to replace it is the single most common cause of a 401 / 404 here.
PLACEHOLDERS = {
    "sk-ant-...",
    "REPLACE_WITH_API_KEY",
    "sk-ant-test-key-not-real",
    "123456:ABC-...",
    "REPLACE_WITH_MOCK_BOT_TOKEN",
    "0000000000:test-token-not-real",
    "your@email.com",
    "yourpassword",
}

OK, WARN, FAIL = "PASS", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def record(status: str, check: str, detail: str) -> None:
    _results.append((status, check, detail))
    print(f"{status:4}  {check:<26} {detail}")


def shape(value: str | None) -> str:
    """Describe a secret without revealing it."""
    if value is None:
        return "<unset>"
    clean = value.strip().strip('"').strip("'")
    notes = []
    if value != clean:
        notes.append("surrounding whitespace/quotes")
    if "\n" in value or "\r" in value:
        notes.append("contains a newline")
    if clean in PLACEHOLDERS:
        notes.append("PLACEHOLDER from a repo template")
    head = clean[:10]
    tail = clean[-4:] if len(clean) > 14 else ""
    return f"len={len(clean)} {head}...{tail}" + (
        "  <-- " + "; ".join(notes) if notes else ""
    )


def source_of(name: str, shell_snapshot: dict[str, str], dotenv_path: str) -> str:
    """Say where the effective value came from: the shell wins over the file."""
    if name in shell_snapshot:
        return "shell export"
    if dotenv_path and name in dotenv_values(dotenv_path):
        return f"{os.path.basename(dotenv_path)}"
    return "unset"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_env_sources(shell: dict[str, str], dotenv_path: str) -> None:
    record(
        OK if dotenv_path else WARN,
        "dotenv file",
        dotenv_path or "none found — relying entirely on shell exports",
    )
    for name in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY"):
        src = source_of(name, shell, dotenv_path)
        value = os.environ.get(name)
        status = FAIL if value is None else OK
        clean = (value or "").strip().strip('"').strip("'")
        if clean in PLACEHOLDERS:
            status = FAIL
        record(status, f"{name}", f"from {src}: {shape(value)}")


def check_auth_config() -> None:
    if os.getenv("ALLOW_ALL_USERS", "").lower() == "true":
        record(WARN, "authorisation", "ALLOW_ALL_USERS=true — anyone can use this bot")
        return
    raw = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").strip()
    if not raw:
        record(
            FAIL,
            "authorisation",
            "ALLOWED_TELEGRAM_USER_IDS is empty — this DENIES everyone",
        )
        return
    try:
        ids = {int(uid.strip()) for uid in raw.split(",") if uid.strip()}
    except ValueError:
        record(FAIL, "authorisation", f"ALLOWED_TELEGRAM_USER_IDS is not numeric: {raw!r}")
        return
    record(OK, "authorisation", f"{len(ids)} allowed user id(s)")


def check_picnic_mode() -> None:
    if os.getenv("PICNIC_MOCK", "").lower() == "true":
        record(OK, "picnic", "PICNIC_MOCK=true — no real Picnic calls, no checkout")
    else:
        record(
            WARN,
            "picnic",
            "PICNIC_MOCK is not true — this will hit the REAL Picnic account",
        )


def check_history_config() -> None:
    raw = os.getenv("MAX_HISTORY_TURNS", "20")
    try:
        turns = int(raw)
    except ValueError:
        record(FAIL, "MAX_HISTORY_TURNS", f"not an integer: {raw!r} (bot will crash at import)")
        return
    if turns <= 0:
        record(WARN, "MAX_HISTORY_TURNS", f"{turns} — effectively disables context")
    else:
        record(OK, "MAX_HISTORY_TURNS", f"{turns} turns = {turns * 2} messages sent to the API")

    persist = os.getenv("PERSIST_CONVERSATIONS", "true").strip().lower()
    on = persist not in ("false", "0", "no")
    record(
        OK if on else WARN,
        "persistence",
        "on" if on else "OFF via PERSIST_CONVERSATIONS — nothing will be stored",
    )


async def check_database() -> None:
    db_path = os.getenv("DB_PATH", "data/picnic.db")
    record(OK, "DB_PATH", os.path.abspath(db_path))

    parent = os.path.dirname(os.path.abspath(db_path)) or "."
    if not os.path.isdir(parent):
        record(FAIL, "database dir", f"{parent} does not exist")
        return

    from sqlalchemy import inspect

    from picnic_meal_planner.db import engine as engine_mod

    try:
        async with engine_mod.get_engine().connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "database", f"cannot open: {type(exc).__name__}")
        return

    needed = {"conversation_messages", "chat_settings"}
    missing = needed - tables
    if missing:
        record(
            FAIL,
            "conversation tables",
            f"missing {sorted(missing)} — run: uv run alembic upgrade head",
        )
    else:
        record(OK, "conversation tables", "conversation_messages, chat_settings present")

    if "alembic_version" in tables:
        record(OK, "alembic", "schema is under migration control")
    else:
        record(
            WARN,
            "alembic",
            "no alembic_version table — created by init_db(); run `alembic stamp head`",
        )


async def check_anthropic() -> None:
    import anthropic

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        record(FAIL, "anthropic api", "no key to test")
        return
    try:
        client = anthropic.AsyncAnthropic(api_key=key)
        await client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=4,
            messages=[{"role": "user", "content": "hi"}],
        )
        record(OK, "anthropic api", "key accepted")
    except anthropic.AuthenticationError:
        record(
            FAIL,
            "anthropic api",
            "401 invalid key — this is what makes /plan fail. Mint a new one and "
            "export ANTHROPIC_API_KEY in this shell",
        )
    except anthropic.NotFoundError as exc:
        record(FAIL, "anthropic api", f"model not found: {str(exc)[:80]}")
    except Exception as exc:  # noqa: BLE001
        record(WARN, "anthropic api", f"could not verify: {type(exc).__name__}")


async def check_telegram() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        record(FAIL, "telegram api", "no token to test")
        return
    try:
        from telegram import Bot

        me = await Bot(token).get_me()
        record(
            OK,
            "telegram api",
            f"token valid — bot is @{me.username}  <-- confirm this is your TEST bot",
        )
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "telegram api", f"{type(exc).__name__}: {str(exc)[:80]}")


# ---------------------------------------------------------------------------

async def main() -> int:
    offline = "--offline" in sys.argv

    # Snapshot the shell before load_dotenv() so we can attribute each value.
    shell = {k: v for k, v in os.environ.items()}
    dotenv_path = find_dotenv(usecwd=True)
    load_dotenv()  # exactly what bot.py does; override=False, so exports win

    print("Preflight for `uv run bot`\n")
    check_env_sources(shell, dotenv_path)
    check_auth_config()
    check_picnic_mode()
    check_history_config()
    await check_database()
    if offline:
        record(WARN, "live api checks", "skipped (--offline)")
    else:
        await check_anthropic()
        await check_telegram()

    fails = [r for r in _results if r[0] == FAIL]
    warns = [r for r in _results if r[0] == WARN]
    print()
    if fails:
        print(f"{len(fails)} blocking problem(s). Fix these before `uv run bot`:")
        for _, check, detail in fails:
            print(f"  - {check}: {detail}")
        return 1
    print(f"All checks passed{f' ({len(warns)} warning(s))' if warns else ''}. "
          "Safe to run `uv run bot`.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
