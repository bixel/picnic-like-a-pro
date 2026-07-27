# Persist conversation history

## Implementation status — RESUME HERE

Implementation was started and then paused partway through. **The feature is not
live yet:** `bot.py` still keeps history in the in-memory dict, so nothing
behaves differently at runtime. The DB layer is in place but inert — no caller
reaches it.

`main` has been merged in. It replaced the hand-written DDL with **SQLAlchemy
2.0 ORM models + Alembic migrations**, so the DB layer below was rewritten to
match; see *Architecture note* immediately after this table.

| Step | Status | Notes |
|---|---|---|
| 1. `db/models.py` models | **Done, verified** | `ConversationMessage` + `ChatSettings` with both indexes. |
| 2. `alembic/versions/0002_conversation_history.py` | **Done, verified** | Applies on top of `0001`; autogenerate reports no drift from the models. |
| 3. `db/queries.py` helpers | **Done, verified** | `load_conversation`, `append_turn`, `list_turns`, `delete_conversation`, `delete_turns`, `delete_turns_in_range`, `get_chat_settings`, `set_chat_persist_history` — all rewritten in SQLAlchemy and commit-neutral. |
| 4. `history.py` | **Done, verified** | `normalize_content`, `is_turn_start`, `trim_history`, lazy cache + `invalidate`, `get_context`/`commit_turn`, deletion, opt-out. |
| 5. `bot.py` wiring | **Not started** ← NEXT | Remove `_histories`/`defaultdict`/`MAX_HISTORY_TURNS`; the 4 changed lines in `_run_claude`. |
| 6. `/forget` + `/privacy` | **Not started** | |
| 7. `.env.example` | **Not started** | `PERSIST_CONVERSATIONS` (`MAX_HISTORY_TURNS` is already documented in `CLAUDE.md`). |
| 8. Docs | **Not started** | `CLAUDE.md` ("Conversation history is kept **per chat_id in memory** … lost on restart") and `ARCHITECTURE.md:60` both go stale the moment step 5 lands. |
| Tests | **Not started** | Port the throwaway round-trip script into `tests/`; see *Verification*. |

Steps 1-4 were exercised against a real SQLite DB and all pass. **Next step is
wiring `bot.py` (step 5)** — until that lands, nothing in the feature is
reachable at runtime.

### Architecture note — what the `main` merge changed

The plan below was written against the old hand-rolled `db/schema.py` +
`aiosqlite` layer, which no longer exists. What actually got built:

| Plan said | Now |
|---|---|
| DDL constants in `db/schema.py`, appended to `ALL_TABLES` | ORM models in `db/models.py`, plus an explicit Alembic migration |
| `CREATE TABLE IF NOT EXISTS` on every connect | `alembic upgrade head` in production; `init_db()` (`create_all`) for fresh installs |
| Helpers take a raw `conn`, some commit internally | Helpers take an `AsyncSession` named `session` and are **commit-neutral** — the caller owns the transaction (per the session contract in `CLAUDE.md`) |
| Add `_post_init` to `bot.py` | Already there — `main` added it. Extend it rather than duplicating it |

Consequence for step 4: every `history.py` call site is
`async with get_db() as session: ... await session.commit()`. The deletion
helpers no longer commit on their own, so `history.py` must commit explicitly.

Schema changes from here follow the documented workflow: edit `db/models.py`,
`uv run alembic revision --autogenerate -m "..."`, review, `uv run alembic upgrade head`.

## Context

Conversation history between the user, the bot, and the Anthropic API currently lives **only in process memory**:

```python
# src/picnic_meal_planner/bot.py:53
_histories: dict[int, list[dict]] = defaultdict(list)
```

Every restart of the container wipes all context for every family member. The SQLite DB at `data/picnic.db` (a Docker named volume) already persists products/orders/order_items/import_checkpoints, but has no conversation tables. `ARCHITECTURE.md:60` documents the in-memory design as intentional; this change supersedes it.

Goal: conversations survive restarts, each chat can opt out of storage and delete what's stored, and the change stays tightly isolated so it can be re-applied after `main` moves.

**Decisions made with the user:**
- **Retention: keep everything by default.** The full transcript is archived; only what is *sent to the API* is truncated. Nothing expires on its own.
- **Deletion is a first-class capability.** The user must be able to delete a whole conversation *or* selected parts (e.g. a given day). No UI for the partial case yet, but the data model must support it from the start.
- **Opt-out: build it now**, per chat, with `/forget` and `/privacy` Telegram commands plus a global env kill-switch.

---

## Design summary

| Decision | Choice | Why |
|---|---|---|
| Shape | `conversation_messages` (row per message) + `chat_settings` | Rows are individually addressable and deletable; a per-chat JSON blob would make partial deletion a read-modify-write of a growing document. |
| Deletable unit | **A turn**, not a message | See below — this is the load-bearing constraint. |
| Normal writes | Append-only, once per successful turn | Each message is stored exactly once and never rewritten. Deletion is a separate, out-of-band operation. |
| Ordering | `id` (AUTOINCREMENT) | Appends mean insertion order *is* conversation order. |
| Serialization | Normalize SDK blocks to plain dicts at append time | One representation serves both the DB and the next API call. |
| Load | Lazy per chat, tail only (`LIMIT 2 × MAX_HISTORY_TURNS`) | The archive can be large; only the recent window is ever sent to the API. |
| Truncation | Cut only at a safe turn boundary | Fixes a live bug (below). |
| Isolation | New `history.py` module | Keeps the `bot.py` diff to ~8 lines. |

### Why the deletable unit is a turn

One `_run_claude` call can produce many messages: `user` → `assistant`(tool_use) → `user`(tool_result) → `assistant`(text). Deleting an arbitrary *message* out of that leaves a `tool_result` with no matching `tool_use`, and the Messages API rejects the next request with a 400.

So every message gets a `turn_id`, and deletion always operates on whole turns. Because each turn begins with a plain-text `user` message, removing any set of turns leaves a sequence that is still valid — user/assistant/user/assistant — with no orphaned blocks. Day-based deletion is then "delete every turn that started on day X", which is naturally turn-aligned.

### Two bugs this fixes along the way

1. **Orphaned `tool_result` → API 400.** `bot.py:106-107` slices blindly (`messages[-(MAX_HISTORY_TURNS*2):]`). That can leave a `tool_result` as the first message with no matching `tool_use`. Rare today (history resets constantly); routine once history persists, and reachable again via deletion.
2. **Mixed types in `_histories`.** `bot.py:128` appends `response.content` — a list of pydantic `TextBlock`/`ToolUseBlock` objects, not dicts. Not JSON-serializable, so this must be normalized before anything can be stored.

---

## Steps 1-3 — the DB layer (DONE)

Implemented and verified against a real SQLite DB. The original hand-written
DDL is superseded by the SQLAlchemy + Alembic layer; read the code rather than
a stale copy of it here.

- **`src/picnic_meal_planner/db/models.py`** — `ConversationMessage`
  (`id`, `chat_id`, `turn_id`, `role`, `content` JSON-as-Text, `created_at`)
  with `idx_conversation_messages_chat` on `(chat_id, id)` for sequential reads
  and `idx_conversation_messages_time` on `(chat_id, created_at)` for day-range
  deletes; and `ChatSettings` (`chat_id` PK, `persist_history`, `updated_at`).
  Both docstrings state the turn-as-deletion-unit rule.
- **`alembic/versions/0002_conversation_history.py`** — additive migration on
  top of `0001`. Touches no existing table, so it is safe on a populated DB and
  safe to roll back.
- **`src/picnic_meal_planner/db/queries.py`** — `load_conversation`,
  `append_turn`, `list_turns`, `delete_conversation`, `delete_turns`,
  `delete_turns_in_range`, `get_chat_settings`, `set_chat_persist_history`.

Two behaviours worth knowing before writing `history.py`:

- **Commit-neutral.** None of these commit; the caller does. This follows the
  session contract in `CLAUDE.md` and lets `history.py` batch a delete and a
  settings write into one transaction.
- **`delete_turns_in_range` resolves the range to whole turns first** (via
  `HAVING MIN(created_at) ...`), then deletes by `turn_id`. Deleting rows by
  timestamp directly would split a turn straddling the boundary and orphan a
  `tool_result`.

## Step 4 — `src/picnic_meal_planner/history.py` (DONE)

The only file that knows about policy. Nothing here imports `bot`.

### Config

```python
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "20"))   # moved out of bot.py
MAX_HISTORY_MESSAGES = MAX_HISTORY_TURNS * 2

def _persistence_enabled_globally() -> bool:
    return os.getenv("PERSIST_CONVERSATIONS", "true").strip().lower() not in ("false", "0", "no")
```

### Serialization

```python
def normalize_content(content: Any) -> str | list[dict]:
    """Convert Anthropic SDK content into plain JSON-serializable data."""
```
Strings pass through; dicts pass through; SDK blocks go through `block.model_dump(mode="json", exclude_none=True)` (the SDK uses pydantic v2). `mode="json"` guarantees primitives; `exclude_none` drops nullable fields the API doesn't need echoed back. Fall back to `.to_dict()`, else raise `TypeError`.

> If extended thinking is ever enabled, thinking blocks must be echoed back byte-identical. `model_dump(mode="json", exclude_none=True)` preserves `signature`, so this stays correct — but add a test then.

### Safe truncation

```python
def is_turn_start(message: dict) -> bool:
    """True if this message can safely be the first one sent to the API:
    a `user` message that is plain text, or whose blocks contain no tool_result."""

def trim_history(messages: list[dict], max_messages: int) -> list[dict]:
    """Trim to at most max_messages, cutting only at a turn start.
    Scans forward from the naive cut point, so it may drop more than strictly
    necessary — always the safe direction. Returns [] if no safe boundary exists."""
```

Guarantees the current slice violates: the first message is never an `assistant` message, and never contains an orphaned `tool_result`. This also runs on load, so a tail left ragged by a deletion can't produce a bad request.

**Call-order change:** trim the *base* history first, then append the new user message. Today `bot.py` appends first and trims after, which can slice away the message the user just sent when the previous turn was tool-heavy.

### State

```python
_contexts: dict[int, list[dict]] = {}   # in-memory API window per chat
_loaded_chats: set[int] = set()         # a defaultdict can't distinguish "unloaded" from "empty"
_persist_flags: dict[int, bool] = {}    # per-chat opt-out cache
```

### Public API

```python
async def get_context(chat_id: int) -> list[dict]:
    """In-memory window, lazily loaded from the DB tail on first use."""

async def commit_turn(chat_id: int, messages: list[dict], new_from: int) -> None:
    """Promote `messages` to the in-memory window and append messages[new_from:]
    to the archive as one turn."""

# --- deletion (all invalidate the in-memory cache) ---

async def clear_history(chat_id: int) -> int:
    """Forget a chat entirely: in-memory window and every stored row."""

async def delete_turns(chat_id: int, turn_ids: Iterable[int]) -> int:
    """Delete specific turns. No command wired up yet; this is the seam."""

async def delete_day(chat_id: int, day: date) -> int:
    """Delete every turn that started on `day` (UTC). Wraps delete_turns_in_range."""

async def list_turns(chat_id: int) -> list[dict]:
    """Turn summaries for a chat, for a future 'what can I delete?' view."""

def invalidate(chat_id: int) -> None:
    """Drop the cached window so the next get_context reloads from the DB.
    Every deletion path calls this — otherwise a deleted turn keeps living in
    memory and gets re-sent to the API until the process restarts."""

# --- opt-out ---

async def is_persistence_enabled(chat_id: int) -> bool:
    """Global env kill-switch AND per-chat chat_settings.persist_history."""

async def set_persistence(chat_id: int, enabled: bool) -> None:
    """Persist the per-chat flag, refresh the cache, and delete stored rows when disabling."""
```

Behaviour:
- `get_context` — if already loaded, return the cached list. Otherwise, if persistence is off, cache `[]`; else `load_conversation(conn, chat_id, limit=MAX_HISTORY_MESSAGES)` and run the result through `trim_history` before caching.
- `commit_turn` — always updates `_contexts[chat_id]` (trimmed) and `_loaded_chats`; then, only if `await is_persistence_enabled(chat_id)`, calls `append_turn` with the new slice.
- `is_persistence_enabled` — env check first (no I/O), then `_persist_flags` cache, then one `get_chat_settings` query. A missing row means enabled (default-on). Result cached.
- **Fail-soft is deliberate.** `get_context` and `commit_turn` never raise: DB errors are logged and degrade to memory-only. `_chat()` already wraps `_run_claude` in a broad `except`; if these raised, an unwritable DB would kill every conversation instead of just losing persistence. Deletion functions *do* propagate errors — silently failing to delete when a user asked you to is the wrong default.

## Step 5 — `src/picnic_meal_planner/bot.py` ← NEXT

Remove `from collections import defaultdict` (line 12), `MAX_HISTORY_TURNS` (line 50), and `_histories` (lines 52-53). Add `from . import history`.

In `_run_claude` (lines 96-147), four changed lines:

```python
    # Trim BEFORE appending the new turn, and only at a safe boundary, so the
    # request can never start with an orphaned tool_result (API 400).
    base = await history.get_context(chat_id)                                 # CHANGED
    messages = history.trim_history(base, history.MAX_HISTORY_MESSAGES - 1)   # CHANGED
    new_from = len(messages)                                                  # CHANGED
    messages.append({"role": "user", "content": user_text})

    while True:
        response = await client.messages.create(...)   # unchanged
        ...
        # Normalize SDK pydantic blocks to plain dicts: JSON-serializable for
        # storage, and accepted verbatim by the API on the way back in.
        messages.append({                                                     # CHANGED
            "role": "assistant",
            "content": history.normalize_content(response.content),
        })
        ...
        # Final response — commit (memory + archive) and return text
        await history.commit_turn(chat_id, messages, new_from)                # CHANGED
        return assistant_text or "(no reply)"
```

`messages[new_from:]` is exactly the messages created this turn — which is also precisely the group that shares a `turn_id`. `tool_uses` keeps holding SDK objects (`tu.name`/`tu.input`/`tu.id` are read directly) — unchanged.

**Startup** — add a `post_init` so a bad `DB_PATH` fails at boot rather than on the first message, and wire it with `.post_init(_post_init)` in `main()`:

```python
async def _post_init(app: Application) -> None:
    from .db.queries import get_db  # noqa: PLC0415
    async with get_db():
        pass  # get_db() runs init_db(), creating any missing tables
    logger.info("DB ready; conversation persistence: %s",
                "on" if history._persistence_enabled_globally() else "off (kill-switch)")
```

## Step 6 — opt-out and deletion commands in `bot.py`

Both follow the existing handler shape (`_is_allowed` guard → `_reject_unauthorized`).

```python
async def cmd_forget(update, context) -> None:
    """Delete this chat's stored history and start fresh."""
    await history.clear_history(update.effective_chat.id)
    await update.message.reply_text("Forgotten — we're starting fresh.")

async def cmd_privacy(update, context) -> None:
    """/privacy → show status; /privacy on|off → set it."""
    chat_id = update.effective_chat.id
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        enabled = await history.is_persistence_enabled(chat_id)
        await update.message.reply_text(
            f"Saving our conversation is {'ON' if enabled else 'OFF'}.\n"
            "Use /privacy off to stop saving (this also deletes what's stored), "
            "or /privacy on to resume. /forget clears the history either way."
        )
        return
    await history.set_persistence(chat_id, arg == "on")
    ...
```

Register both in `main()` and add them to `cmd_start`'s help text. Turning persistence **off also deletes stored rows** for that chat — say so explicitly in the reply.

Partial deletion (`/forget 2026-07-24`, or an interactive turn picker built on `list_turns`) is **not wired up now** — but `delete_day`, `delete_turns`, and `list_turns` exist and are tested, so adding the command later is a handler and nothing else.

## Step 7 — `.env.example`

Under `# App config`:

```dotenv
# Conversation history
MAX_HISTORY_TURNS=20            # messages sent to the API per chat = 2 × this
PERSIST_CONVERSATIONS=true      # global kill-switch; false = in-memory only
```

`MAX_HISTORY_TURNS` is already read by the code today but was never documented.

## Step 8 — docs

Update `ARCHITECTURE.md`: the multi-user note at line 60 ("stored in memory per `chat_id`") is now wrong, and the schema section should gain the two new tables, the turn-as-deletion-unit rule, and the `/forget` + `/privacy` commands.

---

## Privacy notes

Worth stating plainly, since retention is unbounded by default:

- Stored content is sensitive — dietary preferences, household routines, plus `tool_result` payloads carrying the family's full order history and prices. It lands **unencrypted** in the Docker named volume.
- Nothing expires on its own; the archive grows without bound. Realistically small for a family bot, and deletion is available at chat, turn, and day granularity. A scheduled sweep can be added later using `delete_turns_in_range` with no schema change.
- SQLite `DELETE` leaves data in free pages until the file is vacuumed. For a "really delete it" guarantee, run `VACUUM` after a bulk delete; worth noting, not worth doing on every `/forget`.
- `tool_result` content is `str(result)` of a raw tool return — a `search_products` result can be tens of KB and is stored indefinitely. Capping it (`str(result)[:N]`) would shrink both the request and the row, but it changes model-visible behaviour, so flagging rather than doing.

## Risks

- **Concurrency.** PTB processes updates per chat sequentially by default, so two `_run_claude` calls for one `chat_id` shouldn't interleave — which also keeps `MAX(turn_id) + 1` race-free. If that changes, add a per-chat `asyncio.Lock`. Not needed now.
- **Multi-process.** The in-memory cache is per-process; a second replica would go stale, and `invalidate()` would only affect the local one. Single container today — note it in a comment in `history.py`.
- **Rollback.** Purely additive DDL; reverting to the previous image simply ignores the new tables.

## Merge surface (for the next time `main` moves)

The SQLAlchemy/Alembic merge is done. What it cost, for calibration: `schema.py`
was deleted upstream and `queries.py` conflicted, but the *design* survived
untouched — only its expression in code changed. The turn model, the deletion
semantics, and the truncation rules were unaffected.

Remaining exposure, since steps 4-8 are still unwritten:

- **`history.py`** is a new file and cannot conflict.
- **`bot.py`** is the real risk — `_run_claude` and `main()`. If `main` rewrites
  it, re-application is mechanical: trim-then-append at the top,
  `normalize_content` on the assistant append, `commit_turn(chat_id, messages,
  new_from)` at the return, and register the two commands.
- **`db/models.py` + a new Alembic revision** if the schema needs to change
  again. If someone else adds `0003`, renumber rather than branching the
  migration history.
- **`queries.py`** — the conversation section is appended at the end, which is
  the lowest-conflict position available.

---

## Verification

### Already done (steps 1-3, the DB layer)

Exercised against a real SQLite DB with a throwaway script, not committed. All
passed; port these assertions into `tests/` when the test harness lands:

- `alembic upgrade head` applies `0001` → `0002` cleanly.
- `alembic revision --autogenerate` produces an **empty** migration — the models
  and `0002` agree.
- `alembic upgrade head` and `init_db()`/`create_all` produce byte-identical
  schemas for both new tables. (This initially differed: the migration had
  `server_default="1"` on `persist_history` where the model had only a
  Python-side `default=1`. Fixed by adding `server_default` to the model.)
- `append_turn` → `load_conversation` round-trips a tool-using turn **unchanged**,
  including `tool_use` blocks and their nested `input` dicts.
- Turn ids are monotonic per chat; `load_conversation(limit=N)` returns the most
  recent N, oldest-first.
- `list_turns` groups correctly, counts messages per turn, and previews the
  opening user message.
- `delete_turns` on a 4-message tool turn removes exactly those 4 rows, leaves
  neighbours intact, leaves **no orphaned `tool_result`**, and leaves a valid
  turn start at index 0.
- `delete_turns_in_range` removes whole turns; a non-matching range is a no-op
  rather than a wipe.
- `set_chat_persist_history` inserts then upserts; `get_chat_settings` returns
  `None` when unset.

### Already done (step 4, `history.py`)

51 assertions against a real DB, plus a fail-soft suite. Highlights:

- `normalize_content` dumps SDK blocks, drops `None` fields, passes strings and
  plain dicts through, falls back to `to_dict`, and raises on unknown types.
  Output is `json.dumps`-able.
- `trim_history` cuts only at a turn start. The test builds the exact history
  where a naive slice *would* orphan a `tool_result` and asserts it doesn't.
- History survives a simulated restart with `tool_use` input dicts intact.
- **Deleting a turn does not leave it lingering in the live cache** — the
  invalidation path the plan called the easiest thing to get wrong.
- Opting out deletes stored rows but keeps the in-progress conversation in
  memory; opting back in resumes writing.
- The `PERSIST_CONVERSATIONS` kill-switch stops all reads and writes while
  leaving in-session memory working.
- **Fail-soft asymmetry:** with an unwritable DB, `get_context`/`commit_turn`
  degrade to memory-only and `is_persistence_enabled` fails *closed* (never
  store when consent can't be confirmed), while `clear_history`/`delete_turns`
  propagate — silently failing to delete is the wrong default.

**Not yet verified:** anything above `history.py`. `bot.py` is unchanged, so
none of the end-to-end behaviour below has been observed.

### Manual end-to-end (the real acceptance test)

Point `DB_PATH` at a scratch file, start the bot.

1. Send `/plan`, then 2-3 follow-ups referencing earlier context. Confirm coherent replies.
2. `sqlite3 <db> "SELECT id, turn_id, role, substr(content,1,60) FROM conversation_messages ORDER BY id;"` — expect valid JSON, assistant rows like `[{"type": "text", ...}]`, and one `turn_id` shared by every message of an exchange.
3. **Restart the process**, ask "what were we just talking about?" — it must remember. This is the requirement.
4. **Archive accumulates:** chat past `MAX_HISTORY_TURNS`, restart, chat again — early messages are still present even though they're no longer sent to the API.
5. **Tool round-trip:** `/forecast` or `/cart` (forces tool use), restart, ask a follow-up. Verify a `tool_use` and its matching `tool_result` share a `turn_id`, are both stored, and the next call doesn't 400.
6. **Turn-granular deletion:** with a tool-heavy turn in the middle of a conversation, call `delete_turns` on it (via `uv run python -c ...`), then send a message. Verify the request doesn't 400 and the remaining sequence still alternates user/assistant.
7. **Day deletion:** call `delete_day` for today, confirm every row from that day is gone, no partial turns remain (`SELECT turn_id, COUNT(*) ... GROUP BY turn_id` shows no turn with a missing head), and the bot reloads a coherent (empty or older) context on the next message.
8. **Cache invalidation:** delete a turn *without* restarting the bot, then send a message — the deleted content must not come back. This is the easiest thing to get wrong.
9. **Truncation:** set `MAX_HISTORY_TURNS=2`, run a tool-heavy turn plus several more messages. Verify no `400 invalid_request_error`.
10. **Error path:** point `ANTHROPIC_API_KEY` at garbage, send a message. Expect the friendly error reply and **no new rows** (no half-written turn).
11. **Kill-switch:** `PERSIST_CONVERSATIONS=false`, restart, chat — no new rows, in-session memory still works.
12. **Opt-out:** `/privacy` shows ON; `/privacy off` deletes this chat's rows and stops writing; `/privacy on` resumes; `/forget` clears history mid-conversation and the bot visibly loses context.
13. **Degrades, doesn't break:** `chmod 000` the DB, send a message — bot replies normally, exception logged.

**Automated tests.** There is no `tests/` dir and no dev dependency group today. Add `[dependency-groups] dev = ["pytest>=8", "pytest-asyncio>=0.24"]` and `tests/test_history.py`, run via `uv run pytest`.

Pure functions (no fixtures, no DB) — this is where a silent API 400 or a `TypeError: Object of type TextBlock is not JSON serializable` would originate:
- `trim_history` never returns a list starting with an `assistant` message.
- `trim_history` never returns a list whose first message holds a `tool_result` (construct a history where the naive slice *would* orphan one).
- `trim_history` returns input unchanged under the limit, and `[]` for `max_messages=0`.
- `normalize_content` round-trips through `json.dumps` for a fake block exposing `model_dump`.
- `normalize_content` passes plain strings and dicts through untouched.
- `is_turn_start` is `False` for a user message whose content is a `tool_result` block.

DB round-trips against an in-memory SQLite connection — deletion is destructive and correctness matters more than the harness cost, so `pytest-asyncio` is worth taking on here:
- `append_turn` → `load_conversation` round-trips a tool-heavy turn unchanged, with one shared `turn_id`.
- `delete_turns` removes every row of a turn and none of its neighbours.
- `delete_turns_in_range` never leaves a partial turn, including a turn straddling the boundary.
- After deleting a middle turn, `load_conversation` output passes `is_turn_start` at index 0 and still alternates roles.
