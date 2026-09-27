"""Show or switch the LLM model used by each chat.

Each chat can pin its own OpenRouter model; a chat without one follows the
deployment default (``LLM_MODEL``). This is the backend switch until users get
a picker of their own.

Usage:
    uv run python scripts/chat_model.py list
    uv run python scripts/chat_model.py set <chat_id> <model>   # e.g. openai/gpt-5
    uv run python scripts/chat_model.py reset <chat_id>         # back to LLM_MODEL

Changes apply from the chat's next message; the bot does not need a restart.
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

from picnic_meal_planner import llm  # noqa: E402
from picnic_meal_planner.db.engine import init_db  # noqa: E402
from picnic_meal_planner.db.queries import get_db, list_chat_models  # noqa: E402


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

    await init_db()
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
