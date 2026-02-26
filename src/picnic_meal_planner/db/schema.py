"""SQLite table definitions. Called once at startup to ensure schema exists."""

CREATE_PRODUCTS = """
CREATE TABLE IF NOT EXISTS products (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    unit_price    INTEGER,          -- cents
    unit_quantity TEXT,             -- e.g. "1 kg", "6 stuks"
    image_id      TEXT,
    category      TEXT,
    last_seen_at  TEXT              -- ISO datetime
);
"""

CREATE_ORDERS = """
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    picnic_order_id TEXT UNIQUE,    -- Picnic delivery ID, nullable for manual entries
    ordered_at      TEXT NOT NULL,  -- ISO datetime
    delivered_at    TEXT,           -- ISO datetime, nullable
    total_price     INTEGER,        -- cents
    notes           TEXT
);
"""

CREATE_ORDER_ITEMS = """
CREATE TABLE IF NOT EXISTS order_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id TEXT    NOT NULL REFERENCES products(id),
    quantity   INTEGER NOT NULL,
    unit_price INTEGER              -- price at time of order, cents
);
"""

CREATE_IMPORT_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS import_checkpoints (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    last_delivery_id TEXT,          -- Picnic delivery ID of last successfully imported delivery
    imported_at      TEXT NOT NULL, -- ISO datetime of the import run
    total_imported   INTEGER NOT NULL DEFAULT 0,
    finished         INTEGER NOT NULL DEFAULT 0  -- 0 = more pages remain, 1 = fully done
);
"""

CREATE_MEAL_PLANS = """
CREATE TABLE IF NOT EXISTS meal_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    planned_for TEXT NOT NULL,      -- ISO date YYYY-MM-DD
    meal_type   TEXT NOT NULL,      -- breakfast | lunch | dinner | snack
    name        TEXT NOT NULL,      -- e.g. "Spaghetti Bolognese"
    servings    INTEGER,
    notes       TEXT
);
"""

CREATE_MEAL_PLAN_ITEMS = """
CREATE TABLE IF NOT EXISTS meal_plan_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_plan_id INTEGER NOT NULL REFERENCES meal_plans(id) ON DELETE CASCADE,
    product_id   TEXT    NOT NULL REFERENCES products(id),
    quantity     INTEGER NOT NULL
);
"""

ALL_TABLES = [
    CREATE_PRODUCTS,
    CREATE_ORDERS,
    CREATE_ORDER_ITEMS,
    CREATE_IMPORT_CHECKPOINTS,
    CREATE_MEAL_PLANS,
    CREATE_MEAL_PLAN_ITEMS,
]


async def init_db(conn) -> None:
    """Create all tables if they don't already exist."""
    for statement in ALL_TABLES:
        await conn.execute(statement)
    await conn.commit()
