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

Nine ORM models with SQLAlchemy 2.0 `DeclarativeBase` + typed `Mapped[]`
columns:

| Model | Table | Notes |
|---|---|---|
| `Product` | `products` | Picnic product catalogue cache; `category_id` → `categories` |
| `Category` | `categories` | Picnic leaf (L3) categories: id = L3 id, plus `l2_id`, `l1_id`, `name` |
| `CategoryParent` | `category_parents` | Names of Picnic's L1 and L2 categories, keyed by `(level, id)` |
| `Order` | `orders` | Delivery records |
| `OrderItem` | `order_items` | Line items, FK → orders (CASCADE) + products |
| `ImportCheckpoint` | `import_checkpoints` | Import progress log, written only by runs that import something (what to import is read from `orders`) |
| `ConversationMessage` | `conversation_messages` | Chat history; grouped by `turn_id`, the unit of deletion |
| `ChatSettings` | `chat_settings` | Per-chat `persist_history` opt-out and `model` (NULL = `LLM_MODEL`) |
| `PicnicAuth` | `picnic_auth` | The Picnic session token (one row); a credential — never log it |
| `ApiRequest` | `api_requests` | Append-only log of Picnic/OpenRouter HTTP calls; URLs, headers and bodies are sensitive |

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
| `alembic/versions/0004_*.py` | Picnic session token (`picnic_auth`) — purely additive |
| `alembic/versions/0005_*.py` | API request log (`api_requests`) — purely additive |
| `alembic/versions/0006_*.py` | HTTP method, URL and headers on `api_requests` — purely additive |
| `alembic/versions/0007_*.py` | `api_requests.chat_id` for consent-aware deletion — purely additive |
| `alembic/versions/0008_*.py` | Category ids (`categories`, `products.category_id`); **drops** `products.category` — names are looked up again, downgrade restores them from `categories` |
| `alembic/versions/0009_*.py` | L1/L2 names (`category_parents`); fills NULL `categories.l1_id` from siblings — additive |

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

> **Note:** `create_all` bypasses Alembic's migration history tracking, and
> it never adds a column to an existing table.  To bring such a database under
> Alembic, stamp the revision its schema **actually matches**, then upgrade:
> ```bash
> uv run alembic stamp 0002     # or whichever revision the tables match
> uv run alembic upgrade head
> ```
> Never `stamp head` blindly: it records every migration as applied without
> running any, so a missing column stays missing.  `MIGRATION_OPENROUTER.md`
> step 3 shows how to tell which revision a database matches.
>
> The bot refuses to start (`SchemaOutOfDateError`) when
> `engine.pending_migration()` finds Alembic's revision behind
> `engine.SCHEMA_REVISION` — checked *before* `init_db()`, which would
> otherwise create the new tables and make `alembic upgrade head` fail with
> "table already exists" — or when `engine.missing_columns()` reports drift,
> and `scripts/chat_model.py` refuses to run — a stale schema would otherwise
> silently disable history storage.

---

## Upsert Strategy

SQLite's `INSERT … ON CONFLICT DO UPDATE/NOTHING` dialect is used via
`sqlalchemy.dialects.sqlite.insert`.  This keeps the insert operations
idempotent for re-imports.

- `upsert_product`: full upsert (update all mutable columns on conflict).
  It never touches the category; see "Picnic Categories".
- `upsert_category`: insert, or update the name (and `l1_id` when given) of
  an existing L3 — unless its stored `l2_id` differs, see below.
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
├── api_log.py        # Best-effort Picnic/OpenRouter request logging (URLs, headers, bodies)
├── mcp_server.py     # FastMCP server; 15 tools across Picnic, DB, forecasting
├── db/
│   ├── __init__.py   # Re-exports: Base, models, get_db, init_db
│   ├── engine.py     # Async engine, session factory, init_db()
│   ├── models.py     # SQLAlchemy ORM models
│   └── queries.py    # Query helpers (accept AsyncSession, return plain dicts)
├── picnic/
│   ├── auth.py       # Stored session token + password/2FA login flow
│   └── client.py     # Thin async wrapper around python-picnic-api2
└── forecasting/
    └── engine.py     # Urgency-score model (pure Python, no DB access)
```

`calm/picnic-like-a-pro.architecture.json` is a FINOS CALM model of this
structure (nodes, relationships, flows, controls); `calm/README.md` maps it
to the code.  When you add, remove or rewire a module, external service or
guarantee, update it in the same change and run
`calm validate -a picnic-like-a-pro.architecture.json -u url-mapping.json --strict`
from `calm/`.

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

One `_run_llm` call can produce several messages:

```
user  →  assistant (tool_use)  →  user (tool_result)  →  assistant (text)
```

Deleting an arbitrary *message* out of that leaves a `tool_result` with no
matching `tool_use`, and the API rejects the next request with a 400.
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

`_run_llm` discards, rather than persists, a turn whose final assistant
message contains an unanswered `tool_use` (generation cut off mid-block, so
`stop_reason` is `max_tokens` and the tool branch never runs) or whose content
is empty.  Storing either would poison the chat permanently: the next request
400s, and no later turn can repair it because `commit_turn` only runs on
success.

### Serialization

`llm.create_message()` returns `LLMResponse.content` as plain, JSON-safe
Anthropic-shaped dicts, built once in `llm.from_openai_completion()`.  That one
representation is appended to the conversation, sent back on the next request
and stored as-is — nothing downstream converts it.  Keep it that way: if a
new provider path ever yields SDK objects, convert them in `llm.py`.

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

## Picnic Categories

Picnic's categories are a three-level tree (L1 → L2 → L3); products sit in an
L3.  Search tiles and delivery articles carry no category, and Picnic removed
both category endpoints (`get_categories`, `get_article_category`), so
`scripts/import_history.py` looks categories up afterwards, on every run, for
products whose `category_id` is still NULL.

- **`categories.name` is the only copy of an L3 name.**  `products` holds just
  `category_id`; queries join to get the name, so renaming a category renames
  it for every product at once.  A product with no `category_id` has no
  category and is not listed by `get_categories`.
- **Where the data lives** (from `python-picnic-api2`): the product page's
  `category-button` links to `app.picnic://categories/{l1}/l2/{l2}/l3/{l3}`
  (`client.get_category_ids()`, one request).  The L3 name is the
  sub-header `vertical-article-tiles-sub-header-{l3}` on the L2 page
  (`client.get_l3_names()`, one request, every L3 on the page).  The client
  fetches that page itself (`client.L2_PAGE_PATH`) because the library's
  `get_category_by_ids` raises and drops the page when the L3 is missing.
- **L1 and L2 names live in `category_parents`** and come from the L1 page
  (`client.L1_PAGE_PATH`, `client.get_l1_names()`, not in the library): its
  `header.title` is the L1 name, and items
  `core-list-item-category-{l2}` are labelled with the L2 names (curated
  `CustomCat...` items are skipped).  One request per L1; `backfill_parent_names`
  only asks for an L1 with a name still missing.  An L2 its L1 page does not
  list stays unnamed and costs that L1 one request per run.
- **A block stops the lookups.**  Picnic blocks with an HTML 403, which the
  library fails on as `requests.exceptions.JSONDecodeError`; the backfills stop
  rather than try every remaining product.
- **The backfill owns the cache.**  It starts from the stored names, fetches
  an L2 page only for an unknown L3, at most once per `(l2, l3)` per run, and
  stores every L3 named on it, so sibling categories cost nothing later.
- **L3 names are not unique** (e.g. "Alkoholfrei" under several L2s), so
  `categories` is keyed by L3 id — assumed unique across the catalogue.
  `upsert_category` checks that: if a stored L3 turns up under another
  `l2_id`, it logs a warning and keeps the stored row, and
  `set_product_category` leaves the product without a category rather than
  file it under another parent.
- **Not done yet:** names are never refreshed once stored.
  `scripts/inspect_category.py <product_id> --save DIR` dumps the pages, e.g.
  to check whether an L2 page lists every L3 whatever `l3_category_id` is sent.
- The `get_categories` MCP tool lists the local categories with their ids
  and parents' names and, given a name or `category_id`, the products in one;
  there is no live category listing any more.

---

## Picnic Login and 2FA

Picnic may demand a one-time code (2FA) on a password login, and it cannot
be turned off.  So the password is used as rarely as possible; policy lives
in `picnic/auth.py`, and `client.py` applies it.

- **Session token first.** A successful login stores its token in
  `picnic_auth`, and every process (bot, MCP server, import script,
  preflight) starts from it.  The password is used only when none is stored.
- **No unprompted codes.** A password login that hits 2FA raises
  `PicnicLoginRequired` instead of requesting a code; texting the owner on
  every tool call would be worse than an error.  After the first 2FA demand
  the client fails fast without logging in again, but still re-reads the DB,
  so a token stored by another process takes effect without a restart.
- **The message is the UI.** MCP tools surface exceptions as
  `{"error": str(exc)}`, so `PicnicLoginRequired`'s message is written for the
  end user and names both ways to log in.
- **Two front ends, one `LoginFlow`:** `scripts/picnic_login.py` (shell) and
  `/picnic_login [sms|email]` + `/picnic_code <code>` (Telegram).  The
  library verifies the code against the same `PicnicAPI` instance that asked
  for it, so a pending login is an object in memory with a 10-minute TTL; a
  wrong code keeps it pending.
- **A pending login blocks every other password login.**  A new one would
  replace the half-authenticated session the code belongs to.  So while one
  waits, `get_client()` and anyone else's `/picnic_login` raise
  `PicnicLoginPending` (`LoginFlow.ensure_idle()`); only the user who started
  it may restart it, e.g. to switch to email.
- **Tokens are kept current.** `_call()` stores the token whenever the
  session adopts a new one.  A rejected token is cleared — only if it is still
  the stored one, so a fresh login from another process is never wiped — and
  becomes `PicnicLoginRequired`.
- **Failure behaviour** follows `history.py`: reading or writing the token
  degrades (log, continue in memory); clearing propagates.

---

## API Request Log

`api_requests` records HTTP calls to the two external APIs, written by
`api_log.py`.  Instrumentation is at each provider's single choke point, not at
call sites: Picnic's `requests.Session` is wrapped at creation (so every normal
operation, login and 2FA call is captured), and `llm.create_message` records
its OpenRouter call.  Both go through one wrapper,
`api_log.capture_http_requests()`; login steps go through
`auth.login_step()`, whichever front end starts them.

- **Requests are replayable.** A row stores `http_method`, the full `url`
  (including query string), `request_headers`/`response_headers` and the
  complete `request_body`/`response_body`.  Bodies are **not truncated**.  For
  OpenRouter, response headers are not exposed by the SDK, so that field is
  NULL; request headers, URL and body are complete.
- **Credentials are masked, not omitted.** Header values for
  `Authorization`, `x-picnic-auth`, cookies and any name containing `token`,
  `secret`, `api-key`/`apikey` or ending in `-auth` become `[REDACTED]` (a
  `Bearer`/`Basic` scheme is preserved).  Body keys such as `password`,
  `secret`, `otp`, `token` and `api_key` are redacted recursively.  To replay
  a call, substitute the real credential back into the stored curl command.
- **The table is sensitive.** It contains chat prompts, tool results and
  order details.  Protect the database file.  `scripts/import_history.py`
  prunes rows older than `API_LOG_RETENTION_DAYS` (default 30, `0` = keep)
  at the end of every run via `api_log.prune_expired()`; without a scheduled
  import, call it (or `db.queries.prune_api_requests`) from another task.
  `API_LOG_BODIES=false` disables body capture; URLs and headers are still
  stored.
- **Consent follows history.** `bot._run_llm` opens
  `api_log.request_context("bot", chat_id=…, store_bodies=…)` per turn, with
  `store_bodies` from `history.is_persistence_enabled` (fail closed).  A chat
  that opted out gets rows without bodies or URL query strings, and an
  OpenRouter call outside any context stores no bodies.  `api_log` decides
  nothing about consent itself.
- **Deletion reaches the log.** Rows carry `chat_id`; `clear_history`
  (`/forget`), `set_persistence(False)`, `delete_turns` and `delete_day`
  delete the chat's rows in the same transaction.  A turn's messages are all
  stamped at commit, after its requests, so a turn's requests are those in
  `(previous turn's commit, its commit]` (`queries.turn_time_windows`); a
  discarded turn's requests fall into the next turn's window.
- **Other fields:** `provider`, `operation`, `target` (delivery/product id or
  model id), `status`, `http_status`, `duration_ms`, `error_type`,
  `error_message` (truncated), `source`, `run_id`, token counts and
  `requested_at`.  `db.queries.list_api_requests()` filters by provider,
  source and status.
- **Best effort.** A failed log write is a warning and never changes the
  outcome of the request being logged — the opposite default from `history.py`
  deletion.  The insert is awaited, so a row is committed before the caller
  returns.  It uses a separate engine (`engine.get_log_db()`) whose lock
  timeout is `LOG_LOCK_TIMEOUT_SECONDS` (0.2 s): a database locked by another
  connection costs a lost row, not a 5 s stall on every API call.
- **Run grouping.** `api_log.request_context(source)` uses a contextvar;
  `scripts/import_history.py` wraps its whole run, so all rows share a
  `source` (`import_history`) and one `run_id`.  Bot turns use source `bot`
  and a `run_id` per turn; MCP-server calls have NULL there.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `data/picnic.db` | Path to the SQLite file |
| `PICNIC_USERNAME` | — | Picnic account email |
| `PICNIC_PASSWORD` | — | Picnic account password |
| `PICNIC_COUNTRY_CODE` | — | `NL`, `DE`, or `BE` |
| `PICNIC_LOGIN_USER_IDS` | — | Telegram user ids allowed to run `/picnic_login` and `/picnic_code` (each login sends the owner a code); empty = every allowed user |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `OPENROUTER_API_KEY` | — | OpenRouter API key (all LLM requests) |
| `LLM_MODEL` | `anthropic/claude-sonnet-4.6` | Default OpenRouter model id for chats without their own |
| `LLM_MAX_TOKENS` | `4096` | Output token cap per request |
| `LLM_ALLOWED_MODELS` | — | Comma-separated allowlist for per-chat models; empty = any |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Override only for a proxy/gateway |
| `OPENROUTER_APP_NAME` / `OPENROUTER_APP_URL` | `Picnic Like a Pro` / — | Optional attribution headers |
| `API_LOG_BODIES` | `true` | Store full request/response bodies in `api_requests`; `false` = no bodies (URLs/headers still stored) |
| `API_LOG_RETENTION_DAYS` | `30` | `import_history.py` prunes older `api_requests` rows; `0` = keep everything |
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
uv run python scripts/import_history.py  # import new Picnic deliveries (re-runnable)
uv run alembic upgrade head           # apply all pending migrations
uv run alembic revision --autogenerate -m "msg"  # generate migration from model diff
uv run alembic downgrade -1           # roll back one migration
uv run python scripts/chat_model.py list          # per-chat models
uv run python scripts/chat_model.py set <chat_id> <model>
uv run python scripts/picnic_login.py             # log in to Picnic (2FA), store the session
uv run python scripts/picnic_login.py --status    # is the stored session valid?
uv run python scripts/inspect_category.py <product_id> [--save DIR]  # dump category pages
```
