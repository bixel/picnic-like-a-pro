"""Conversation history: policy, caching, and persistence.

This module owns everything about *how* conversation history is kept. ``bot.py``
only calls :func:`get_context` and :func:`commit_turn`; the DB layer only stores
what it is handed. Nothing here imports ``bot``.

Three responsibilities:

1. **Serialization** — the Anthropic SDK returns pydantic content blocks, which
   are not JSON-serializable. :func:`normalize_content` converts them to plain
   dicts once, at append time, so the same representation serves both the API
   and the database.
2. **Safe truncation** — only the most recent window is sent to the API, but the
   cut must land on a real turn boundary. See :func:`trim_history`.
3. **Persistence policy** — lazy per-chat loading, a write-through cache, and
   the opt-out checks.

The in-memory cache is per-process. That is fine for the current
single-container deployment; a second replica would see stale caches and
:func:`invalidate` would only affect the local one.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from .db.queries import (
    append_turn,
    delete_conversation,
    delete_turns_in_range,
    get_chat_settings,
    get_db,
    load_recent_turns,
    set_chat_persist_history,
)
from .db.queries import delete_turns as _delete_turns_rows
from .db.queries import list_turns as _list_turns_rows

logger = logging.getLogger(__name__)

# Number of *messages* kept in the API context window. Note that a turn using
# tools produces 4+ messages, so the number of conversational turns actually
# retained is lower than MAX_HISTORY_TURNS. This mirrors the pre-existing
# behaviour; only the truncation boundary has changed.
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "20"))
MAX_HISTORY_MESSAGES = MAX_HISTORY_TURNS * 2


def _persistence_enabled_globally() -> bool:
    """Global kill-switch: PERSIST_CONVERSATIONS=false disables all storage."""
    return os.getenv("PERSIST_CONVERSATIONS", "true").strip().lower() not in (
        "false",
        "0",
        "no",
    )


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def normalize_content(content: Any) -> str | list[dict]:
    """Convert Anthropic SDK content into plain JSON-serializable data.

    ``response.content`` is a list of pydantic models (``TextBlock``,
    ``ToolUseBlock``, ...). The Messages API accepts plain dicts on the way back
    in, so normalizing here gives one representation for both the next request
    and the database row.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content

    blocks: list[dict] = []
    for block in content:
        if isinstance(block, dict):
            blocks.append(block)
        elif hasattr(block, "model_dump"):
            # exclude_none drops nullable fields the API does not need echoed
            # back; mode="json" guarantees primitives all the way down.
            blocks.append(block.model_dump(mode="json", exclude_none=True))
        elif hasattr(block, "to_dict"):
            blocks.append(block.to_dict())
        else:
            raise TypeError(f"Cannot serialize content block of type {type(block)!r}")
    return blocks


# ---------------------------------------------------------------------------
# Safe truncation
# ---------------------------------------------------------------------------

def is_turn_start(message: dict) -> bool:
    """True if *message* can safely be the first one sent to the API.

    That means a ``user`` message that is plain text, or whose blocks contain no
    ``tool_result`` — a tool_result with no preceding tool_use is a 400.
    """
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    return not any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in (content or [])
    )


def trim_history(messages: list[dict], max_messages: int) -> list[dict]:
    """Trim to at most *max_messages*, cutting only at a real turn start.

    Scans forward from the naive cut point, so it may drop more than strictly
    necessary — always the safe direction. Returns ``[]`` when no safe boundary
    exists inside the window, which starts the conversation fresh rather than
    sending a request the API would reject.

    The head is validated **even when the input already fits**. An earlier
    version returned a short list untouched, which made this function a no-op
    in precisely the case it was written for: a list that is already at or
    under the limit but begins mid-turn.
    """
    if max_messages <= 0:
        return []
    for i in range(max(0, len(messages) - max_messages), len(messages)):
        if is_turn_start(messages[i]):
            return list(messages[i:])
    return []


# ---------------------------------------------------------------------------
# In-memory state
#
# _loaded_chats is what distinguishes "not yet read from the DB" from
# "genuinely empty" — a defaultdict cannot express that difference.
# ---------------------------------------------------------------------------

_contexts: dict[int, list[dict]] = {}
_loaded_chats: set[int] = set()
_persist_flags: dict[int, bool] = {}

# append_turn allocates a turn id with MAX(turn_id) + 1, which is read-then-
# write and therefore racy: concurrent commits for one chat would all read the
# same maximum and collapse into a single turn, silently merging conversations
# that later get deleted together.
#
# python-telegram-bot happens to serialise updates process-wide today (it
# awaits each one unless `concurrent_updates` is set), so the race is not
# reachable — but that is a property of the caller's configuration, not a
# guarantee this module should depend on. Setting `concurrent_updates=True` in
# bot.py would otherwise silently corrupt turn grouping.
_turn_locks: dict[int, asyncio.Lock] = {}


def _turn_lock(chat_id: int) -> asyncio.Lock:
    return _turn_locks.setdefault(chat_id, asyncio.Lock())


def invalidate(chat_id: int) -> None:
    """Drop the cached window so the next :func:`get_context` re-reads the DB.

    Every deletion path calls this. Without it a deleted turn keeps living in
    memory and gets re-sent to the API until the process restarts.
    """
    _contexts.pop(chat_id, None)
    _loaded_chats.discard(chat_id)


def _reset_caches() -> None:
    """Drop every cached chat. Equivalent to a process restart, for tests."""
    _contexts.clear()
    _loaded_chats.clear()
    _persist_flags.clear()
    _turn_locks.clear()


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------

async def is_persistence_enabled(chat_id: int) -> bool:
    """True when both the global kill-switch and this chat's flag allow storage."""
    if not _persistence_enabled_globally():
        return False
    if chat_id in _persist_flags:
        return _persist_flags[chat_id]

    enabled = True  # default-on: a chat with no settings row is opted in
    try:
        async with get_db() as session:
            settings = await get_chat_settings(session, chat_id)
        if settings is not None:
            enabled = bool(settings["persist_history"])
    except Exception:  # noqa: BLE001 — never let a DB fault break a chat
        logger.exception("Could not read chat settings for chat %d", chat_id)
        return False  # fail closed: don't store when we can't confirm consent

    _persist_flags[chat_id] = enabled
    return enabled


async def set_persistence(chat_id: int, enabled: bool) -> None:
    """Set this chat's opt-out flag.

    Disabling also deletes everything already stored for the chat — opting out
    of storage while leaving the old transcript on disk would be surprising.
    The in-memory window is deliberately left intact so the conversation in
    progress stays coherent; it simply stops being written down.
    """
    async with get_db() as session:
        await set_chat_persist_history(session, chat_id, enabled)
        if not enabled:
            await delete_conversation(session, chat_id)
        await session.commit()
    _persist_flags[chat_id] = enabled


# ---------------------------------------------------------------------------
# Read / write
#
# get_context and commit_turn are fail-soft: a database problem degrades to
# memory-only operation rather than breaking the conversation. The deletion
# helpers below deliberately propagate instead — silently failing to delete
# something a user asked to have deleted is the wrong default.
# ---------------------------------------------------------------------------

async def get_context(chat_id: int) -> list[dict]:
    """Return the in-memory API window, lazily loading from the DB on first use."""
    if chat_id in _loaded_chats:
        return _contexts.setdefault(chat_id, [])

    messages: list[dict] = []
    if await is_persistence_enabled(chat_id):
        try:
            async with get_db() as session:
                # Loads whole turns, so the window cannot begin mid-turn.
                stored = await load_recent_turns(
                    session, chat_id, max_messages=MAX_HISTORY_MESSAGES
                )
            # Belt and braces: repairs a head left ragged by a skipped corrupt
            # row, which turn-aware loading alone does not cover.
            messages = trim_history(stored, MAX_HISTORY_MESSAGES)
        except Exception:  # noqa: BLE001
            logger.exception("Could not load history for chat %d", chat_id)
            messages = []

    _contexts[chat_id] = messages
    _loaded_chats.add(chat_id)
    return messages


async def commit_turn(chat_id: int, messages: list[dict], new_from: int) -> None:
    """Promote *messages* to the in-memory window and archive the new slice.

    ``messages[new_from:]`` is exactly what this turn produced, which is also
    precisely the group that shares one ``turn_id``.
    """
    _contexts[chat_id] = trim_history(messages, MAX_HISTORY_MESSAGES)
    _loaded_chats.add(chat_id)

    new_messages = messages[new_from:]
    if not new_messages or not await is_persistence_enabled(chat_id):
        return

    try:
        # Serialises turn-id allocation for this chat; see _turn_locks above.
        async with _turn_lock(chat_id):
            async with get_db() as session:
                await append_turn(session, chat_id, new_messages)
                await session.commit()
    except Exception as exc:  # noqa: BLE001
        # Deliberately not logger.exception: a DBAPIError traceback can carry
        # the statement being executed, and the row here is the message itself.
        logger.error(
            "Could not persist turn for chat %d: %s", chat_id, type(exc).__name__
        )


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

async def clear_history(chat_id: int) -> int:
    """Forget a chat entirely: the in-memory window and every stored row.

    The database is cleared first so that a failure leaves both sides
    untouched, rather than wiping memory and reporting an error.
    """
    async with get_db() as session:
        removed = await delete_conversation(session, chat_id)
        await session.commit()

    _contexts[chat_id] = []
    _loaded_chats.add(chat_id)  # loaded-and-empty; no point re-reading the DB
    return removed


async def delete_turns(chat_id: int, turn_ids: Iterable[int]) -> int:
    """Delete specific turns in full. Returns the number of messages removed."""
    async with get_db() as session:
        removed = await _delete_turns_rows(session, chat_id, turn_ids)
        await session.commit()
    invalidate(chat_id)
    return removed


async def delete_day(chat_id: int, day: date) -> int:
    """Delete every turn that *started* on *day* (UTC).

    ``created_at`` is an ISO-8601 string, so a plain date prefix compares
    correctly as a half-open string range.
    """
    async with get_db() as session:
        removed = await delete_turns_in_range(
            session,
            chat_id,
            since=day.isoformat(),
            until=(day + timedelta(days=1)).isoformat(),
        )
        await session.commit()
    invalidate(chat_id)
    return removed


async def list_turns(chat_id: int) -> list[dict]:
    """Turn summaries for a chat, for a future "what can I delete?" view."""
    async with get_db() as session:
        return await _list_turns_rows(session, chat_id)
