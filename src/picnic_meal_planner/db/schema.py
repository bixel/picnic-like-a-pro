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

CREATE_CONVERSATION_MESSAGES = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,   -- Telegram chat id
    turn_id    INTEGER NOT NULL,   -- groups all messages of one exchange; unit of deletion
    role       TEXT    NOT NULL,   -- 'user' | 'assistant'
    content    TEXT    NOT NULL,   -- JSON: a string, or a list of content blocks
    created_at TEXT    NOT NULL    -- ISO datetime
);
"""

CREATE_CONVERSATION_MESSAGES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_conversation_messages_chat
    ON conversation_messages (chat_id, id);
"""

# Supports deleting everything from a given day, and turn lookups by time.
CREATE_CONVERSATION_MESSAGES_TIME_INDEX = """
CREATE INDEX IF NOT EXISTS idx_conversation_messages_time
    ON conversation_messages (chat_id, created_at);
"""

CREATE_CHAT_SETTINGS = """
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id         INTEGER PRIMARY KEY,
    persist_history INTEGER NOT NULL DEFAULT 1,  -- 0 = chat opted out of persistence
    updated_at      TEXT    NOT NULL             -- ISO datetime
);
"""

ALL_TABLES = [
    CREATE_PRODUCTS,
    CREATE_ORDERS,
    CREATE_ORDER_ITEMS,
    CREATE_IMPORT_CHECKPOINTS,
    CREATE_CONVERSATION_MESSAGES,
    CREATE_CONVERSATION_MESSAGES_INDEX,
    CREATE_CONVERSATION_MESSAGES_TIME_INDEX,
    CREATE_CHAT_SETTINGS,
]


async def init_db(conn) -> None:
    """Create all tables if they don't already exist."""
    for statement in ALL_TABLES:
        await conn.execute(statement)
    await conn.commit()
