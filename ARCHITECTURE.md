# Picnic Like a Pro — Architecture & Implementation Plan

## Overview

A family meal planner and grocery assistant built on top of the Picnic grocery API.
The system exposes Picnic capabilities as MCP tools to a Claude-powered Telegram bot,
with a local SQLite database that tracks full order history and supports forecasting
future orders based on historical purchasing patterns.

---

## Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        Telegram User                            │
└───────────────────────────┬─────────────────────────────────────┘
                            │ messages
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Telegram Bot                               │
│              (python-telegram-bot v22)                          │
│   Passes each conversation turn to Claude via Anthropic API     │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Anthropic API (claude-sonnet-4-6)
                            │ + MCP tools (in-process)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                       MCP Server                                │
│                  (FastMCP / stdio transport)                     │
│                                                                 │
│  Picnic tools          DB tools             Forecast tools      │
│  ─────────────         ────────             ──────────────      │
│  search_products       get_order_history    forecast_order      │
│  get_cart              get_frequent_items   reorder_due_items   │
│  add_to_cart           record_order         ─────────────────   │
│  remove_from_cart      get_product_stats                        │
│  clear_cart            ─────────────────                        │
│  get_delivery_slots                                             │
│  get_categories                                                 │
└──────────┬──────────────────────┬───────────────────────────────┘
           │                      │
           ▼                      ▼
┌──────────────────┐   ┌─────────────────────────────────────────┐
│   Picnic API     │   │            SQLite Database               │
│ (python-picnic-  │   │  (aiosqlite)                             │
│    api2)         │   │                                          │
│                  │   │  products       order_history            │
│  NL / DE / BE    │   │  order_items    meal_plans               │
└──────────────────┘   │  meal_plan_items                        │
                        └─────────────────────────────────────────┘
```

---

## Project Structure

```
picnic-like-a-pro/
├── pyproject.toml              # uv project + dependencies
├── .python-version             # Python 3.11
├── .env.example                # Required env vars template
├── .gitignore
│
├── src/
│   └── picnic_meal_planner/
│       ├── __init__.py
│       │
│       ├── bot.py              # Telegram bot entry point
│       ├── mcp_server.py       # FastMCP server (all tools)
│       │
│       ├── db/
│       │   ├── __init__.py
│       │   ├── schema.py       # Table definitions (CREATE TABLE statements)
│       │   ├── seed.py         # Seeds DB with historical order data
│       │   └── queries.py      # Async query helpers
│       │
│       ├── picnic/
│       │   ├── __init__.py
│       │   └── client.py       # Thin wrapper around python-picnic-api2
│       │
│       └── forecasting/
│           ├── __init__.py
│           └── engine.py       # Order frequency analysis + forecasting
│
├── scripts/
│   └── import_history.py       # One-time script: pull real order history
│                               # from Picnic API and persist to DB
│
└── data/
    └── .gitkeep                # DB file lives here (gitignored)
```

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
| category     | TEXT    |                                |
| last_seen_at | TEXT    | ISO datetime                   |

### `orders`
One row per Picnic delivery. Can be seeded from Picnic delivery history or
recorded as new orders are placed through the bot.

| Column          | Type    | Notes                          |
|-----------------|---------|--------------------------------|
| id              | INTEGER PK AUTOINCREMENT |               |
| picnic_order_id | TEXT    | Picnic delivery ID (nullable for manually entered) |
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

### Conversation model
Each message in a chat session is forwarded to the Anthropic API with:
- Full conversation history (last N turns, configurable)
- All MCP tools available
- A system prompt describing the assistant's role as a family grocery/meal planner

Claude autonomously decides which MCP tools to call and presents results conversationally.

---

## Environment Variables

```dotenv
# Picnic credentials
PICNIC_USERNAME=your@email.com
PICNIC_PASSWORD=yourpassword
PICNIC_COUNTRY_CODE=NL        # NL, DE, or BE

# Telegram
TELEGRAM_BOT_TOKEN=123456:ABC-...

# Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# App config
DB_PATH=data/picnic.db
ALLOWED_TELEGRAM_USER_IDS=123456789,987654321   # comma-separated, leave empty to allow all
```

---

## Dependency Management (uv)

```toml
# pyproject.toml (key sections)
[project]
name = "picnic-meal-planner"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "python-picnic-api2>=1.3",
    "mcp[cli]>=1.9",
    "anthropic>=0.40",
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
uv run import-history  # one-time: pull real history from Picnic
```

---

## Implementation Phases

### Phase 1 — Project skeleton & DB
- `pyproject.toml` + `uv` setup
- `db/schema.py` — all CREATE TABLE statements
- `db/queries.py` — async helpers for all tables
- `db/seed.py` — realistic synthetic order history (so forecasting works from day 1)
- `scripts/import_history.py` — pull real delivery history from Picnic API

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

### Phase 5 — Docs & deployment
- `README.md` with setup instructions
- Docker Compose file (optional)
