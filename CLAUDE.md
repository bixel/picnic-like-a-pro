# CLAUDE.md — Architectural Decisions

Guidance for Claude Code (and human contributors) working in this repo.

---

## Project at a Glance

**Picnic Like a Pro** is a multi-user Telegram bot that acts as a family
grocery and meal planning assistant.  It wraps the Picnic grocery API
(NL/DE/BE), stores order history locally, and uses an LLM (any model on
OpenRouter, chosen per chat) with MCP tools to answer natural-language queries.

---

## Database Layer

### ORM: SQLAlchemy 2.0 (async)

All database access goes through **SQLAlchemy 2.0** with the async API.

- Driver: `aiosqlite` (SQLite via `sqlite+aiosqlite:///`)
- Engine: `create_async_engine` — lazy singleton in `db/engine.py`
- Sessions: `async_sessionmaker(AsyncSession, expire_on_commit=False)`
- Sessions are obtained with the `get_db()` async context manager and passed
  explicitly into every query helper (no implicit global session).

### Session Contract

```python
async with get_db() as session:
    await db.some_write(session, ...)
    await session.commit()        # caller's responsibility
```

`save_checkpoint()` is the only query helper that commits internally — it is
always the final write in a batch.  Every other helper is commit-neutral so
that the caller can batch multiple writes into one transaction.

### Models: `db/models.py`

Six ORM models with SQLAlchemy 2.0 `DeclarativeBase` + typed `Mapped[]`
columns:

| Model | Table | Notes |
|---|---|---|
| `Product` | `products` | Picnic product catalogue cache |
| `Order` | `orders` | Delivery records |
| `OrderItem` | `order_items` | Line items, FK → orders (CASCADE) + products |
| `ImportCheckpoint` | `import_checkpoints` | Resumable import progress |
| `ConversationMessage` | `conversation_messages` | Chat history; grouped by `turn_id`, the unit of deletion |
| `ChatSettings` | `chat_settings` | Per-chat `persist_history` opt-out and `model` (NULL = `LLM_MODEL`) |

Datetime values are stored as **ISO-8601 strings** (`String` column type,
not `DateTime`) to avoid SQLite timezone edge-cases and to keep the existing
data format unchanged.

### Foreign Key Enforcement

SQLite does not enforce foreign keys by default.  The engine module attaches a
`"connect"` event listener to the underlying sync engine that executes
`PRAGMA foreign_keys=ON` for every connection.

### Schema Migrations: Alembic

Alembic manages all schema changes.

| File | Purpose |
|---|---|
| `alembic.ini` | Alembic config; DB URL is overridden at runtime from `DB_PATH` |
| `alembic/env.py` | Async migration env using `async_engine_from_config` + `asyncio.run` |
| `alembic/versions/0001_*.py` | Initial schema (products, orders, order_items, import_checkpoints) |
| `alembic/versions/0002_*.py` | Conversation history (`conversation_messages`, `chat_settings`) — purely additive |
| `alembic/versions/0003_*.py` | Per-chat LLM model (`chat_settings.model`, nullable) — purely additive |

**Important flags:**
- `render_as_batch=True` — required for SQLite because it cannot do most
  `ALTER TABLE` operations directly; Alembic uses a table-rebuild strategy.

**Workflow for schema changes:**

```bash
# 1. Edit db/models.py
# 2. Auto-generate a migration
uv run alembic revision --autogenerate -m "describe the change"
# 3. Review & edit the generated file in alembic/versions/
# 4. Apply
uv run alembic upgrade head
```

**Production deploy:**

```bash
uv run alembic upgrade head   # run before (re)starting the app
uv run bot
```

### Development / Fresh Install

When starting the app for the first time (bot, mcp-server, or import script),
`db/engine.py::init_db()` is called at startup.  It runs
`Base.metadata.create_all(checkfirst=True)` — a no-op if tables already
exist, otherwise it creates them.

> **Note:** `create_all` bypasses Alembic's migration history tracking.
> If you later run `alembic upgrade head` on a database bootstrapped this way,
> stamp it first so Alembic doesn't try to re-create tables:
> ```bash
> uv run alembic stamp head
> ```

---

## Upsert Strategy

SQLite's `INSERT … ON CONFLICT DO UPDATE/NOTHING` dialect is used via
`sqlalchemy.dialects.sqlite.insert`.  This keeps the insert operations
idempotent for re-imports.

- `upsert_product`: full upsert (update all mutable columns on conflict).
- `insert_order`: conflict-do-nothing on `picnic_order_id`; returns the
  existing row's id when the row already exists.

---

## Application Structure

```
src/picnic_meal_planner/
├── bot.py            # Telegram bot, long-polling; calls history.py for context
├── llm.py            # OpenRouter client, Anthropic<->OpenAI message translation,
│                     #   per-chat model selection
├── history.py        # Conversation history policy: serialization, truncation,
│                     #   caching, persistence, opt-out, deletion
├── mcp_server.py     # FastMCP server; 15 tools across Picnic, DB, forecasting
├── db/
│   ├── __init__.py   # Re-exports: Base, models, get_db, init_db
│   ├── engine.py     # Async engine, session factory, init_db()
│   ├── models.py     # SQLAlchemy ORM models
│   └── queries.py    # Query helpers (accept AsyncSession, return plain dicts)
├── picnic/
│   └── client.py     # Thin async wrapper around python-picnic-api2
└── forecasting/
    └── engine.py     # Urgency-score model (pure Python, no DB access)
```

Query helpers in `queries.py` return **plain dicts** (not ORM model
instances).  This decouples callers from the ORM and makes the data
JSON-serialisable without extra steps.

---

## AI / MCP Integration

- Every LLM request goes through **OpenRouter** (OpenAI-compatible Chat
  Completions, via the `openai` SDK) in `llm.py` — not via the MCP server
  process.  The MCP tools are registered with FastMCP and called **in-process**
  via `mcp_server.call_tool()`.
- **History stays in the Anthropic content-block shape** (`text` / `tool_use` /
  `tool_result`).  `llm.create_message()` translates it to the OpenAI wire
  format on every request and translates the reply back, so `history.py`'s
  turn invariants and the stored rows are provider-independent.  Do not store
  OpenAI-shaped messages.
- **The model is per chat.** `llm.get_chat_model(chat_id)` returns
  `chat_settings.model`, or `LLM_MODEL` when that is NULL.  It is resolved once
  per turn (never mid tool loop) and deliberately not cached, so a backend
  switch (`scripts/chat_model.py`) applies on the next message.
  `llm.set_chat_model()` enforces `LLM_ALLOWED_MODELS` and is the function a
  future user-facing picker should call; `/model` is read-only today.
- Conversation history is **persisted to SQLite per chat_id** and survives
  restarts.  All policy lives in `history.py`; `bot.py` only calls
  `get_context()` and `commit_turn()`.
  - The full transcript is archived; only the window *sent to the API* is
    capped (`MAX_HISTORY_TURNS * 2` messages, default 40).
  - Messages are grouped into **turns**, and a turn is the unit of deletion —
    deleting a lone message could orphan a `tool_result` and cause an API 400.
  - Storage can be disabled per chat (`chat_settings.persist_history`) or
    globally (`PERSIST_CONVERSATIONS=false`).

---

## Conversation History

All policy lives in `history.py`.  `bot.py` calls only `get_context()` and
`commit_turn()`; `queries.py` stores what it is handed and decides nothing.

### A turn is the unit of deletion

One `_run_claude` call can produce several messages:

```
user  →  assistant (tool_use)  →  user (tool_result)  →  assistant (text)
```

Deleting an arbitrary *message* out of that leaves a `tool_result` with no
matching `tool_use`, and the Messages API rejects the next request with a 400.
So every message carries a `turn_id` and **all deletion operates on whole
turns**.  Because each turn begins with a plain-text `user` message, removing
any set of turns leaves a valid alternating sequence.

Deletion is available by chat (`clear_history`), by turn (`delete_turns`), and
by day (`delete_day`).  Only the first is exposed over Telegram today, via
`/forget`; the others are ready for a UI whenever one is wanted.

### The window must never open mid-turn

Two mechanisms, at different layers:

1. **Loading is turn-aware.** `load_recent_turns()` picks the most recent
   *whole* turns that fit in `MAX_HISTORY_MESSAGES`, using the stored
   `turn_id` rather than inferring boundaries.  A turn too large for the
   window is dropped entirely, never truncated.
2. **`trim_history()` is the backstop.** It scans *forward* from the naive cut
   point to the next real turn start, so it may drop more than strictly
   necessary — always the safe direction — and returns `[]` if no safe
   boundary exists.  It validates the head **even when the input already
   fits**; an earlier version short-circuited on length, which made it a no-op
   in exactly the case it existed for.

`bot.py` trims *before* appending the new user message; trimming afterwards
could discard the message the user just sent.

### A turn is only archived if it ended cleanly

`_run_claude` discards, rather than persists, a turn whose final assistant
message contains an unanswered `tool_use` (generation cut off mid-block, so
`stop_reason` is `max_tokens` and the tool branch never runs) or whose content
is empty.  Storing either would poison the chat permanently: the next request
400s, and no later turn can repair it because `commit_turn` only runs on
success.

### Serialization

`response.content` is a list of pydantic SDK blocks, which are not
JSON-serialisable.  `normalize_content()` converts them once, at append time,
via `model_dump(mode="json", exclude_none=True)`, so a single representation
serves both the next API request and the stored row.

### Failure behaviour

Deliberately asymmetric:

| Operation | On DB failure |
|---|---|
| `get_context`, `commit_turn` | Log and degrade to memory-only — an unwritable DB must not break every conversation |
| `is_persistence_enabled` | Fail **closed** — never store when consent cannot be confirmed |
| `clear_history`, `delete_*` | **Propagate** — silently failing to delete what a user asked to delete is the wrong default.  Callers in `bot.py` catch these and report the failure rather than claiming success |

### Caching

The in-memory window is per-process, so every deletion path calls
`invalidate()`; otherwise a deleted turn keeps living in memory and gets
re-sent to the API until restart.  A second replica would see stale caches —
fine for the current single-container deployment, but it is why the cache is
not treated as authoritative.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `data/picnic.db` | Path to the SQLite file |
| `PICNIC_USERNAME` | — | Picnic account email |
| `PICNIC_PASSWORD` | — | Picnic account password |
| `PICNIC_COUNTRY_CODE` | — | `NL`, `DE`, or `BE` |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `OPENROUTER_API_KEY` | — | OpenRouter API key (all LLM requests) |
| `LLM_MODEL` | `anthropic/claude-sonnet-4.6` | Default OpenRouter model id for chats without their own |
| `LLM_MAX_TOKENS` | `4096` | Output token cap per request |
| `LLM_ALLOWED_MODELS` | — | Comma-separated allowlist for per-chat models; empty = any |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Override only for a proxy/gateway |
| `OPENROUTER_APP_NAME` / `OPENROUTER_APP_URL` | `Picnic Like a Pro` / — | Optional attribution headers |
| `ALLOWED_TELEGRAM_USER_IDS` | — | Comma-separated list of allowed user IDs |
| `ALLOW_ALL_USERS` | `false` | Set to `true` for local dev (bypasses auth) |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `sse`, or `http` |
| `MCP_HOST` | `127.0.0.1` | MCP server host (sse/http only) |
| `MCP_PORT` | `8000` | MCP server port (sse/http only) |
| `IMPORT_BATCH_SIZE` | `10` | Deliveries per batch in import script |
| `IMPORT_DELAY_SECONDS` | `2` | Pause between import batches |
| `MAX_HISTORY_TURNS` | `20` | Per-chat turns sent to the API (× 2 = messages). Does **not** limit what is stored. |
| `PERSIST_CONVERSATIONS` | `true` | Global kill-switch; `false` = in-memory only, nothing written |

Copy `.env.example` to `.env` and fill in your values.

---

## Key Commands

```bash
uv sync                               # install dependencies
uv run bot                            # start Telegram bot
uv run mcp-server                     # start MCP server (stdio)
uv run python scripts/import_history.py  # import Picnic delivery history
uv run alembic upgrade head           # apply all pending migrations
uv run alembic revision --autogenerate -m "msg"  # generate migration from model diff
uv run alembic downgrade -1           # roll back one migration
uv run python scripts/chat_model.py list          # per-chat models
uv run python scripts/chat_model.py set <chat_id> <model>
```
