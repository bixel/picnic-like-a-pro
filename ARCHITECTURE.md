# Picnic Like a Pro — Architecture & Implementation Plan

## Overview

A family meal planner and grocery assistant built on top of the Picnic grocery API.
The system exposes Picnic capabilities as MCP tools to a Claude-powered Telegram bot,
with a local SQLite database that tracks full order history and supports forecasting
future orders based on historical purchasing patterns.

---

## Components

```
┌──────────────────────────────────────────────────────────────────────┐
│           Family members (multiple Telegram users)                   │
│        Alice, Bob, kids — each with their own Telegram account       │
└────────────────────────────┬─────────────────────────────────────────┘
                             │ messages (per-user conversation history)
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        Telegram Bot                                  │
│                  (python-telegram-bot v22)                           │
│  • Per-user conversation state (chat_id → message history)          │
│  • Passes each turn to the chat's LLM via OpenRouter (llm.py)        │
│  • Optional: ALLOWED_TELEGRAM_USER_IDS allowlist                     │
└────────────────────────────┬─────────────────────────────────────────┘
                             │ OpenRouter (per-chat model, default LLM_MODEL)
                             │ + MCP tools (in-process, shared)
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         MCP Server                                   │
│          (FastMCP — stdio for Claude Desktop, SSE for bot)           │
│                                                                      │
│  Picnic tools          DB tools             Forecast tools           │
│  ─────────────         ────────             ──────────────           │
│  search_products       get_order_history    forecast_order           │
│  get_cart              get_frequent_items   reorder_due_items        │
│  add_to_cart           record_order         ──────────────────       │
│  remove_from_cart      get_product_stats                             │
│  clear_cart            ────────────────                              │
│  get_delivery_slots                                                  │
│  get_categories                                                      │
└───────────┬───────────────────────────┬──────────────────────────────┘
            │                           │
            ▼                           ▼
┌───────────────────┐   ┌──────────────────────────────────────────────┐
│    Picnic API     │   │              SQLite Database                  │
│  (python-picnic-  │   │  (aiosqlite)                                  │
│     api2)         │   │                                               │
│                   │   │  products    orders    order_items            │
│  Shared family    │   │  meal_plans  meal_plan_items                  │
│  account          │   │  import_checkpoints  (for stepwise sync)      │
│                   │   │  conversation_messages  chat_settings         │
│  NL / DE / BE     │   │                                               │
└───────────────────┘   └──────────────────────────────────────────────┘
```

### Multi-user design notes
- The **Picnic account is shared** (one family account). Cart and orders are global.
- Each **Telegram user gets their own conversation history** with Claude, stored
  per `chat_id` in SQLite and cached in memory. It survives restarts, and there
  is no cross-user memory leakage. Storage can be disabled per chat or globally.
- All family members talk to the same bot instance and can see/modify the shared cart.
- `ALLOWED_TELEGRAM_USER_IDS` restricts access to known family members. It is
  the only control protecting stored conversations: an empty value denies
  everyone (set `ALLOW_ALL_USERS=true` for local dev instead).

### MCP transport modes
| Mode | Transport | Use case |
|------|-----------|----------|
| Embedded in bot | In-process (direct call) | Normal Telegram operation |
| Standalone server | stdio | Claude Desktop integration |
| Standalone server | SSE / streamable-HTTP | Remote / multi-client access |

The same `mcp_server.py` supports all three — transport is selected at startup via
the `MCP_TRANSPORT` env var (`stdio` / `sse` / `http`). Default is `stdio`.

---

## Project Structure

```
picnic-like-a-pro/
├── pyproject.toml              # uv project + deps + pytest/coverage config
├── .python-version             # Python 3.13
├── .env.example                # Production env vars template
├── .env.mock.example           # Mock environment template
├── .env.test                   # Test environment (no secrets, committed)
├── Makefile                    # Targets for all three environments
├── Dockerfile                  # builder → test → runtime stages
├── docker-compose.yml          # Production
├── docker-compose.mock.yml     # Mock playground
├── docker-compose.test.yml     # Automated tests + coverage
├── .gitignore
│
├── alembic.ini                 # Alembic config (DB URL from DB_PATH at runtime)
├── alembic/
│   ├── env.py                  # Async migration env
│   └── versions/               # 0001 initial schema, 0002 conversation history,
│                               # 0003 per-chat LLM model, 0004 Picnic session
│
├── src/
│   └── picnic_meal_planner/
│       ├── __init__.py
│       │
│       ├── bot.py              # Telegram bot entry point
│       ├── history.py          # Conversation history policy & persistence
│       ├── llm.py              # OpenRouter client, message translation, per-chat model
│       ├── mcp_server.py       # FastMCP server (all tools)
│       │
│       ├── db/
│       │   ├── __init__.py
│       │   ├── engine.py       # Async engine, session factory, init_db()
│       │   ├── models.py       # SQLAlchemy ORM models
│       │   └── queries.py      # Async query helpers
│       │
│       ├── picnic/
│       │   ├── __init__.py
│       │   ├── auth.py         # Stored session token + password/2FA login
│       │   ├── client.py       # Thin wrapper around python-picnic-api2
│       │   └── mock_client.py  # In-process fake Picnic API (PICNIC_MOCK=true)
│       │
│       └── forecasting/
│           ├── __init__.py
│           └── engine.py       # Order frequency analysis + forecasting
│
├── scripts/
│   ├── import_history.py       # Incremental import of Picnic delivery history
│   │                           # (re-runnable, rate-limit safe)
│   ├── chat_model.py           # List / set / reset each chat's LLM model
│   ├── picnic_login.py         # Interactive Picnic login (2FA); stores the session
│   └── preflight.py            # Config + live API checks before `uv run bot`
│
├── tests/
│   ├── conftest.py             # Shared fixtures (mock client, isolated DBs)
│   ├── fixtures/
│   │   ├── history.py          # Seeds 25 orders over ~6 months
│   │   └── conversations.py    # Standard usage scenarios
│   ├── test_mock_client.py
│   ├── test_picnic_client.py
│   ├── test_db_queries.py
│   ├── test_forecasting.py
│   ├── test_mcp_tools.py
│   ├── test_bot_helpers.py
│   ├── test_bot_conversation.py
│   ├── test_history.py         # Conversation history policy & persistence
│   ├── test_db_engine.py
│   ├── test_migrations.py
│   └── test_conversations.py   # End-to-end scenario replays
│
└── data/
    └── .gitkeep                # DB file lives here (gitignored)
```

---

## Environments

Three instances run in parallel, each an isolated Compose project with its own
container, volume, and configuration. Only production ever reaches the real
Picnic API.

| | Production | Mock | Test |
|---|---|---|---|
| Compose file | `docker-compose.yml` | `docker-compose.mock.yml` | `docker-compose.test.yml` |
| Compose project | `picnic-like-a-pro` | `picnic-mock` | `picnic-test` |
| Container | `picnic-bot` | `picnic-bot-mock` | `picnic-test` |
| Volume | `picnic-data` | `picnic-mock-data` | anonymous (ephemeral) |
| Env file | `.env` | `.env.mock` | `.env.test` |
| Picnic API | **real** | mocked | mocked |
| Persistence | permanent | permanent | wiped on demand |
| Telegram bot | production token | separate token | none |

```bash
make prod-up      # production
make mock-up      # persistent playground (make mock-seed to load history)
make test         # local suite + coverage
make test-docker  # containerised suite, matches CI
make mock-reset   # wipe the playground
make test-docker-wipe   # wipe the test environment
```

### Mock mode

`PICNIC_MOCK=true` swaps `PicnicAPI` for `MockPicnicAPI` inside
`picnic/client.py`. Everything above that boundary — MCP tools, forecasting,
bot, database — runs unmodified, so the mock exercises real application code
paths. The mock provides ~50 Dutch grocery products, a stateful cart, delivery
slots, and 25 weekly deliveries generated relative to the current date (most
recent 8 days ago) so forecasts are always meaningful. `PICNIC_MOCK_PERSIST_CART`
keeps the cart across restarts.

No checkout API is exposed by either the real or the mock client, and the test
suite's autouse fixture injects a fresh mock into every test, so tests cannot
reach the network even if `PICNIC_MOCK` is unset.

---

## Database Schema

### `products`
Cached product catalog. Populated on first search/add, updated on use.

| Column       | Type    | Notes                          |
|--------------|---------|--------------------------------|
| id           | TEXT PK | Picnic product/article ID      |
| name         | TEXT    |                                |
| unit_price   | INTEGER | cents                          |
| unit_quantity | TEXT   | e.g. "1 kg", "6 stuks"        |
| image_id     | TEXT    | for display                    |
| category     | TEXT    | L3 category name               |
| category_id  | INTEGER | FK → categories.id (L3 id); NULL = not looked up yet |
| last_seen_at | TEXT    | ISO datetime                   |

### `categories`
Picnic's leaf (L3) categories, filled by the import script's category backfill.
A product page's category button links to
`app.picnic://categories/{l1}/l2/{l2}/l3/{l3}`; the name is the L3 sub-header
on the L2 category page. L3 names repeat under different parents, so the id is
the key.

| Column | Type       | Notes                     |
|--------|------------|---------------------------|
| id     | INTEGER PK | L3 category id            |
| l2_id  | INTEGER    | parent L2 category id     |
| l1_id  | INTEGER    | top-level L1 category id  |
| name   | TEXT       | L3 name                   |

### `orders`
One row per Picnic delivery. Populated by the import script and/or recorded
as new orders are placed through the bot.

| Column          | Type    | Notes                          |
|-----------------|---------|--------------------------------|
| id              | INTEGER PK AUTOINCREMENT |               |
| picnic_order_id | TEXT UNIQUE | Picnic delivery ID (nullable for manually entered) |
| ordered_at      | TEXT    | ISO datetime of when order was placed |
| delivered_at    | TEXT    | ISO datetime of actual delivery (nullable) |
| total_price     | INTEGER | cents                          |
| notes           | TEXT    | free text, e.g. "birthday party week" |

### `order_items`
Line items for each order. Core table for forecasting.

| Column     | Type    | Notes                       |
|------------|---------|-----------------------------|
| id         | INTEGER PK AUTOINCREMENT |             |
| order_id   | INTEGER FK → orders.id    |             |
| product_id | TEXT FK → products.id     |             |
| quantity   | INTEGER |                             |
| unit_price | INTEGER | price at time of order (cents) |

### `import_checkpoints`
A log of history-import progress: one row per batch that imported something,
plus one when such a run ends.  Runs that import nothing write no row.  What to
import is read from `orders`, not from here.

| Column        | Type    | Notes                                   |
|---------------|---------|-----------------------------------------|
| id            | INTEGER PK AUTOINCREMENT |                        |
| last_delivery_id | TEXT | Newest Picnic delivery ID that run imported (a log, never a resume position) |
| imported_at   | TEXT    | ISO datetime of the import run          |
| total_imported | INTEGER | Number of deliveries stored locally after that run |
| finished      | INTEGER | 0 = something was deferred by that run, 1 = nothing left to import |

### `conversation_messages`
Per-chat conversation history with Claude. Messages are grouped into **turns**
(`turn_id`); a turn is everything one exchange produced, and is the unit of
deletion — removing a single message could orphan a `tool_result` block and
cause an API 400.

| Column     | Type    | Notes                                    |
|------------|---------|------------------------------------------|
| id         | INTEGER PK AUTOINCREMENT | Insertion order == conversation order |
| chat_id    | INTEGER | Telegram chat id (no FK; external)       |
| turn_id    | INTEGER | Per-chat, monotonic; groups one exchange |
| role       | TEXT    | `user` or `assistant`                    |
| content    | TEXT    | JSON: a string, or a list of content blocks |
| created_at | TEXT    | ISO datetime                             |

Indexed on `(chat_id, id)` for sequential reads and `(chat_id, created_at)` for
day-range deletion.

### `chat_settings`
Per-chat preferences: the storage opt-out and the LLM model.

| Column          | Type    | Notes                              |
|-----------------|---------|------------------------------------|
| chat_id         | INTEGER PK | Telegram chat id                |
| persist_history | INTEGER | 0 = chat opted out of storage      |
| model           | TEXT NULL | OpenRouter model id; NULL = `LLM_MODEL` (migration 0003) |
| updated_at      | TEXT    | ISO datetime                       |

### `picnic_auth`
The Picnic session token, shared by every process using the account (migration
0004). At most one row: the deployment has one Picnic account.

| Column     | Type       | Notes                                   |
|------------|------------|-----------------------------------------|
| id         | INTEGER PK | Always 1                                |
| auth_token | TEXT       | Session token — a credential, never logged |
| updated_at | TEXT       | ISO datetime                            |

### `meal_plans`
Optional: track planned meals to drive shopping list generation.

| Column      | Type    | Notes                         |
|-------------|---------|-------------------------------|
| id          | INTEGER PK AUTOINCREMENT |              |
| planned_for | TEXT    | ISO date (YYYY-MM-DD)         |
| meal_type   | TEXT    | breakfast / lunch / dinner / snack |
| name        | TEXT    | e.g. "Spaghetti Bolognese"    |
| servings    | INTEGER |                               |
| notes       | TEXT    |                               |

### `meal_plan_items`
Ingredients linked to a meal plan entry.

| Column       | Type    | Notes                        |
|--------------|---------|------------------------------|
| id           | INTEGER PK AUTOINCREMENT |             |
| meal_plan_id | INTEGER FK → meal_plans.id |            |
| product_id   | TEXT FK → products.id      |            |
| quantity     | INTEGER |                              |

---

## MCP Tools

### Picnic API tools
| Tool | Description |
|------|-------------|
| `search_products(query, limit)` | Search Picnic product catalog |
| `get_categories()` | Browse top-level product categories |
| `get_cart()` | Return current cart contents |
| `add_to_cart(product_id, quantity)` | Add N units to cart |
| `remove_from_cart(product_id)` | Remove a product from cart |
| `clear_cart()` | Empty the cart |
| `get_delivery_slots()` | List available delivery windows |

### Order history & DB tools
| Tool | Description |
|------|-------------|
| `get_order_history(limit, days_back)` | Recent orders with line items |
| `get_product_order_stats(product_id)` | Order count, avg quantity, avg interval for one product |
| `get_frequently_ordered(limit)` | Products ranked by order frequency |
| `record_order(items)` | Persist a new order to local DB after checkout |

### Forecasting tools
| Tool | Description |
|------|-------------|
| `forecast_order(horizon_days)` | Products predicted to run out within N days, ranked by priority |
| `get_reorder_due()` | Products whose reorder interval has elapsed since last order |

---

## Forecasting Logic

Initial implementation: **average interval model** (simple, interpretable, no ML deps).

For each product with ≥ 2 historical orders:
1. Compute average days between consecutive orders → `avg_interval`
2. Compute average quantity per order → `avg_quantity`
3. Find date of last order → `last_ordered`
4. `days_since_last = today − last_ordered`
5. `urgency_score = days_since_last / avg_interval`

Products with `urgency_score ≥ 1.0` are overdue. Claude presents these to the user,
sorted by urgency, and can add them to the Picnic cart directly.

This is designed so that a more sophisticated model (e.g. time-series ML, seasonal
decomposition) can replace just the `engine.py` file later without touching anything else.

---

## Telegram Bot Flows

### Commands
| Command | Behaviour |
|---------|-----------|
| `/start` | Welcome message, brief how-to |
| `/plan` | Start a meal planning session with Claude |
| `/order` | Open a shopping session (search, cart management) |
| `/forecast` | Ask Claude what you're likely running low on |
| `/history` | Show recent order summary |
| `/cart` | Show current Picnic cart |
| `/forget` | Delete this chat's stored conversation |
| `/privacy` | Show or change whether the conversation is saved (`on` / `off`) |
| `/model` | Show which LLM model this chat uses |

### Conversation model
Each message in a chat session is forwarded to the chat's model via
OpenRouter's OpenAI-compatible API (`llm.py`) with:
- Recent conversation history (last N turns, configurable via `MAX_HISTORY_TURNS`)
- All MCP tools available
- A system prompt describing the assistant's role as a family grocery/meal planner

The model autonomously decides which MCP tools to call and presents results conversationally.

The model is chosen **per chat**: `chat_settings.model` pins one, and `NULL`
follows the deployment default `LLM_MODEL`. History is stored in the Anthropic
content-block shape and translated to/from the OpenAI wire format on every
request, so a chat can switch model mid-conversation without losing context.

History is **persisted to SQLite** and survives restarts. The full transcript is
archived; only the window sent to the API is capped. Policy lives in
`history.py` — `bot.py` just reads a context and commits a completed turn.

Storage is opt-out at two levels: per chat via `/privacy off` (which also
deletes what is already stored), and globally via `PERSIST_CONVERSATIONS=false`.
Disabling storage does not wipe the in-progress conversation from memory — it
simply stops being written down.

---

## Environment Variables

```dotenv
# Picnic credentials
PICNIC_USERNAME=your@email.com
PICNIC_PASSWORD=yourpassword
PICNIC_COUNTRY_CODE=NL        # NL, DE, or BE

# Telegram
TELEGRAM_BOT_TOKEN=123456:ABC-...

# LLM via OpenRouter
OPENROUTER_API_KEY=sk-or-v1-...
LLM_MODEL=anthropic/claude-sonnet-4.6   # default for chats without their own model
# LLM_MAX_TOKENS=4096
# LLM_ALLOWED_MODELS=                   # comma-separated allowlist for per-chat models

# App config
DB_PATH=data/picnic.db
ALLOWED_TELEGRAM_USER_IDS=123456789,987654321   # comma-separated; leave empty to DENY all

# Conversation history
MAX_HISTORY_TURNS=20          # message cap for the API window = 2 x this (a tool turn is 4+ messages)
PERSIST_CONVERSATIONS=true    # global kill-switch; false = in-memory only

# MCP server (when run standalone)
MCP_TRANSPORT=stdio           # stdio | sse | http
MCP_HOST=127.0.0.1
MCP_PORT=8000

# History import (optional tuning)
IMPORT_BATCH_SIZE=10          # deliveries fetched per API call (default: 10)
IMPORT_DELAY_SECONDS=2        # pause between batches to avoid rate limits (default: 2)
```

---

## Dependency Management (uv)

```toml
# pyproject.toml (key sections)
[project]
name = "picnic-meal-planner"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "python-picnic-api2>=1.3",
    "mcp[cli]>=1.9",
    "openai>=1.40",               # OpenRouter client (OpenAI-compatible API)
    "python-telegram-bot[ext]>=22",
    "aiosqlite>=0.20",
    "python-dotenv>=1.0",
]

[project.scripts]
bot = "picnic_meal_planner.bot:main"
mcp-server = "picnic_meal_planner.mcp_server:main"
import-history = "scripts.import_history:main"
```

Run with:
```bash
uv sync
uv run bot          # start the Telegram bot
uv run mcp-server   # start MCP server standalone (for Claude Desktop)
uv run import-history  # pull history from Picnic (re-run for new deliveries)
```

---

## History Import Script

`scripts/import_history.py` is **optional** and safe to run any number of times,
including on a schedule: each run imports the deliveries that are new since the
last one.

### How it works
1. Every run fetches the full list of past Picnic deliveries (summary only) and
   the set of `picnic_order_id`s already in `orders`.
2. The deliveries with no row in `orders` are the work list — **not** the ones
   after the checkpoint's `last_delivery_id`. Picnic returns the list
   newest-first, so a new delivery appears *above* everything an earlier run
   imported and a positional resume would never see it. Cancelled and
   still-`CURRENT` deliveries are counted and left out of the work list up
   front; a cancelled one never gets a row, so otherwise it would be "new" on
   every run.
3. Iterates the work list in batches of `IMPORT_BATCH_SIZE` (default 10), fetching
   line items for each delivery and inserting into `orders` + `order_items`.
4. After each batch that imported something it writes a checkpoint
   (`import_checkpoints` table) with the newest delivery it imported, and sleeps
   `IMPORT_DELAY_SECONDS` (default 2 s) between batches. The checkpoint is a
   progress log: what to import is read from `orders`.
5. Duplicate prevention: the work list excludes everything already in `orders`,
   which covers a run interrupted between a commit and its batch checkpoint.
   Inside the insert's session the script checks `orders` once more, against an
   overlapping run that imported the same delivery meanwhile. The UNIQUE
   constraint on `picnic_order_id` remains the backstop.
6. Cancelled deliveries are skipped for good. A delivery that cannot be imported
   *yet* — still `CURRENT`, a failed fetch, or a detail that
   `picnic.client.delivery_unusable_reason()` rejects (error body, no order time,
   no order lines) — is **deferred**: nothing is written for it, so it has no row
   in `orders` and the next run retries it; the run is not marked finished.
7. Line items come from `picnic.client.delivery_line_items()`, which flattens the
   real API's `orders → ORDER_LINE → ORDER_ARTICLE` nesting (count from the
   `QUANTITY` decorator, unit price from the line price) as well as the mock's
   flat shape.
8. Prints a progress summary after each batch and, at the end, how many
   deliveries this run imported and how many the database now holds.
9. When nothing is new it prints "Nothing new to import.", writes **no**
   checkpoint (so a scheduled run leaves the database untouched) and moves on to
   the category backfill.

```
$ uv run import-history
Fetching delivery list from Picnic...
Found 47 deliveries: 0 already imported, 47 to import, 0 cancelled, 0 still in progress.

Batch 1/5 (deliveries 1–10 of 47)...  imported 10  [sleeping 2s]
Batch 2/5 (deliveries 11–20 of 47)... imported 10  [sleeping 2s]
^C   ← safe to interrupt here

$ uv run import-history      ← resumes with the 27 that still have no row
Found 47 deliveries: 20 already imported, 27 to import, 0 cancelled, 0 still in progress.

Batch 1/3 (deliveries 1–10 of 27)... imported 10  [sleeping 2s]
...
```

---

## Implementation Phases

### Phase 1 — Project skeleton & DB
- `pyproject.toml` + `uv` setup
- `db/schema.py` — all CREATE TABLE statements including `import_checkpoints`
- `db/queries.py` — async helpers for all tables
- `scripts/import_history.py` — incremental, resumable import of real Picnic history

### Phase 2 — MCP server
- `picnic/client.py` — authenticated Picnic wrapper
- `mcp_server.py` — all tools wired up with FastMCP
- Manual test via `mcp dev`

### Phase 3 — Forecasting engine
- `forecasting/engine.py` — average interval model
- `forecast_order` and `get_reorder_due` MCP tools

### Phase 4 — Telegram bot
- `bot.py` — command handlers + conversation loop
- System prompt tuning
- `ALLOWED_TELEGRAM_USER_IDS` access control

### Phase 5 — Deployment & docs
- `Dockerfile`
- `docker-compose.yml` (service snippet for the VPS)
- `.github/workflows/deploy.yml` — `workflow_dispatch` CI/CD
- `README.md` with setup instructions

---

## Deployment

### Overview

The bot uses **long polling** and exposes no inbound ports. No reverse proxy
configuration is needed. The container simply needs outbound internet access
(Telegram API + Picnic API + OpenRouter).

The SQLite database lives in a named Docker volume mounted at `/app/data`.

```
VPS
├── existing docker-compose (nginx-proxy + your other services)
│
└── picnic-meal-planner/
    ├── docker-compose.yml      ← standalone, independent of nginx-proxy
    ├── .env                    ← secrets, never committed
    └── data/                   ← volume mount point for SQLite DB
```

### Dockerfile

Multi-stage build using the official `uv` Docker image for fast, reproducible installs.

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim-bookworm
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY pyproject.toml ./

ENV PATH="/app/.venv/bin:$PATH"
RUN mkdir -p /app/data

ENTRYPOINT ["uv", "run"]
CMD ["bot"]
```

Running the import script as a one-off (without restarting the bot):
```bash
docker compose run --rm picnic-bot import-history
```

### docker-compose.yml

```yaml
services:
  picnic-bot:
    image: ghcr.io/${GITHUB_REPOSITORY}:latest
    container_name: picnic-bot
    restart: unless-stopped
    env_file: .env
    volumes:
      - picnic-data:/app/data

volumes:
  picnic-data:
```

No ports exposed — the bot is outbound-only (polling).

### Environment file on VPS

```
/home/<user>/picnic-meal-planner/.env
```

Contains all secrets from the `.env.example` template. Never committed to git.
Permissions should be `chmod 600 .env`.

---

## CI/CD

### Strategy

| Trigger | Action |
|---------|--------|
| Manual via `gh workflow run` (from Claude Code or GitHub UI) | Full deploy |
| Manual SSH fallback | `docker compose pull && docker compose up -d` |

There is no automatic deploy on push to `main` — deploys are always intentional.

### GitHub Actions secrets required

| Secret | Value |
|--------|-------|
| `VPS_HOST` | IP or hostname of the VPS |
| `VPS_USER` | SSH user (e.g. `deploy`) |
| `VPS_SSH_KEY` | Private key for SSH access |
| `VPS_COMPOSE_DIR` | Absolute path to the compose directory on VPS |

The workflow uses `GITHUB_TOKEN` (automatic) to push to `ghcr.io` — no extra registry secret needed.

### `.github/workflows/deploy.yml`

```yaml
name: Build & Deploy

on:
  workflow_dispatch:           # trigger manually (e.g. gh workflow run deploy.yml)

env:
  IMAGE: ghcr.io/${{ github.repository }}

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Log in to ghcr.io
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          push: true
          tags: |
            ${{ env.IMAGE }}:latest
            ${{ env.IMAGE }}:${{ github.sha }}

  deploy:
    needs: build-and-push
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to VPS
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            cd ${{ secrets.VPS_COMPOSE_DIR }}
            docker compose pull
            docker compose up -d
            docker image prune -f
```

### Triggering from Claude Code

```bash
gh workflow run deploy.yml
# Watch progress:
gh run watch
```
