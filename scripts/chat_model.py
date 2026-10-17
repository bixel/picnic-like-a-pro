"""Show or switch the LLM model used by each chat.

Each chat can pin its own OpenRouter model; a chat without one follows the
deployment default (``LLM_MODEL``). This is the backend switch until users get
a picker of their own.

Usage:
    uv run python scripts/chat_model.py list
    uv run python scripts/chat_model.py set <chat_id> <model>   # e.g. openai/gpt-5
    uv run python scripts/chat_model.py reset <chat_id>         # back to LLM_MODEL

Changes apply from the chat's next message; the bot does not need a restart.

The script never creates or alters the schema: it refuses to run against a
database that is missing or not fully migrated, so a typo in ``DB_PATH`` cannot
silently produce an empty database.
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

from picnic_meal_planner import llm  # noqa: E402
from picnic_meal_planner.db.engine import get_engine, missing_columns  # noqa: E402
from picnic_meal_planner.db.queries import get_db, list_chat_models  # noqa: E402

UPGRADE_HINT = "run `uv run alembic upgrade head` (see MIGRATION_OPENROUTER.md)"


async def _schema_problem() -> str | None:
    """Why this database cannot be used, or ``None`` when it is fine."""
    db_path = os.getenv("DB_PATH", "data/picnic.db")
    if not os.path.isfile(db_path):
        return f"no database at DB_PATH={db_path!r} — check the path, or start the bot once"
    async with get_engine().connect() as conn:
        tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    absent = {"chat_settings", "conversation_messages"} - tables
    if absent:
        return f"missing tables {sorted(absent)} — {UPGRADE_HINT}"
    missing = await missing_columns()
    if missing:
        return f"missing columns {missing} — {UPGRADE_HINT}"
    return None


async def _list() -> None:
    async with get_db() as session:
        chats = await list_chat_models(session)
    print(f"Default model (LLM_MODEL): {llm.DEFAULT_MODEL}")
    allowed = llm.allowed_models()
    print(f"Allowed models (LLM_ALLOWED_MODELS): {', '.join(sorted(allowed)) or 'any'}\n")
    if not chats:
        print("No chats yet.")
        return
    for chat in chats:
        print(f"{chat['chat_id']:>16}  {chat['model'] or '(default)'}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show every chat and its model")
    p_set = sub.add_parser("set", help="pin a model for one chat")
    p_set.add_argument("chat_id", type=int)
    p_set.add_argument("model", help="OpenRouter model id, e.g. anthropic/claude-sonnet-4.6")
    p_reset = sub.add_parser("reset", help="make a chat follow LLM_MODEL again")
    p_reset.add_argument("chat_id", type=int)
    args = parser.parse_args()

    problem = await _schema_problem()
    if problem:
        print(f"Database not usable: {problem}", file=sys.stderr)
        return 1
    if args.cmd == "list":
        await _list()
        return 0
    try:
        await llm.set_chat_model(args.chat_id, args.model if args.cmd == "set" else None)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Chat {args.chat_id} now uses {await llm.get_chat_model(args.chat_id)}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
