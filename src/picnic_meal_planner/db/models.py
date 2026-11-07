"""SQLAlchemy ORM models for the picnic-meal-planner database.

All datetime values are stored as ISO-8601 strings (TEXT in SQLite) to keep
the schema simple and compatible with the existing data. Use
``datetime.fromisoformat()`` / ``.isoformat()`` when working with them in
Python.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Category(Base):
    """A leaf (L3) category of Picnic's three-level category tree.

    A product page links to its category as
    ``app.picnic://categories/{l1}/l2/{l2}/l3/{l3}``; the name is the L3
    sub-header on the L2 category page.  L3 names are not unique (e.g.
    "Alkoholfrei" can sit under more than one L2), so the id is the key.
    """

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)  # L3 id
    l2_id: Mapped[int] = mapped_column(Integer, nullable=False)
    l1_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)

    products: Mapped[list[Product]] = relationship(back_populates="category_ref")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    unit_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_quantity: Mapped[str | None] = mapped_column(String, nullable=True)
    image_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The L3 category's name, kept on the row for name-based lookups; the
    # category itself is ``category_id``.  Rows that predate category ids
    # (or seeded mock data) may have a name and no id.
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("categories.id"), nullable=True
    )
    last_seen_at: Mapped[str | None] = mapped_column(String, nullable=True)  # ISO datetime

    order_items: Mapped[list[OrderItem]] = relationship(back_populates="product")
    category_ref: Mapped[Category | None] = relationship(back_populates="products")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    picnic_order_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    ordered_at: Mapped[str] = mapped_column(String, nullable=False)   # ISO datetime
    delivered_at: Mapped[str | None] = mapped_column(String, nullable=True)  # ISO datetime
    total_price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # cents
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(
        String, ForeignKey("products.id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # price at order time, cents

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(back_populates="order_items")


class ImportCheckpoint(Base):
    __tablename__ = "import_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    last_delivery_id: Mapped[str | None] = mapped_column(String, nullable=True)
    imported_at: Mapped[str] = mapped_column(String, nullable=False)  # ISO datetime
    total_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    finished: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0 or 1


class ConversationMessage(Base):
    """One message in a chat's conversation with Claude.

    Messages are grouped into *turns*: a turn is everything produced by a
    single exchange, which for a tool-using turn is several messages
    (user -> assistant/tool_use -> user/tool_result -> assistant/text).

    A turn is the unit of deletion. Removing an individual message would leave
    a ``tool_result`` with no matching ``tool_use``, which the Anthropic API
    rejects with a 400; removing whole turns always leaves a valid sequence.

    There is deliberately no FK to anything — ``chat_id`` is a Telegram id,
    external to this database.
    """

    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # 'user' | 'assistant'
    # JSON-encoded: either a bare string, or a list of content-block dicts.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)  # ISO datetime

    __table_args__ = (
        # Sequential read of a chat's history (insertion order == conversation order).
        Index("idx_conversation_messages_chat", "chat_id", "id"),
        # "Delete everything from day X" and other time-range lookups.
        Index("idx_conversation_messages_time", "chat_id", "created_at"),
    )


class ChatSettings(Base):
    """Per-chat preferences: the history-persistence opt-out and the LLM model."""

    __tablename__ = "chat_settings"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 0 = this chat opted out of having its conversation stored. server_default
    # keeps metadata.create_all() and the Alembic migration in exact agreement.
    persist_history: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # OpenRouter model id for this conversation, e.g. "openai/gpt-5". NULL means
    # "follow the deployment default" (LLM_MODEL), so changing the default
    # moves every chat that has not chosen a model of its own.
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)  # ISO datetime


class PicnicAuth(Base):
    """The Picnic session token, shared by every process using the account.

    At most one row (``id = 1``): the deployment has one Picnic account.
    Storing the token means a restart reuses the session instead of logging
    in with the password again, which on a 2FA account would demand a new
    code every time.  The token is a credential: never log it.
    """

    __tablename__ = "picnic_auth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    auth_token: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)  # ISO datetime


class ApiRequest(Base):
    """One logged call to an external API (Picnic or OpenRouter).

    Append-only observability data: ``api_log.py`` writes rows best-effort and
    nothing ever updates them.  A row records the HTTP method, full URL,
    request/response headers and the complete request/response bodies, so a
    logged call can be replayed with curl after substituting the real
    credentials.

    **This table contains sensitive data** (chat prompts, tool results, order
    details).  Credential values — the Picnic password/2FA code/session token
    and the OpenRouter API key — are replaced with ``[REDACTED]``, but the rest
    of the headers and bodies is verbatim and is **not truncated**.  Treat the
    database file accordingly and prune the log with
    ``db.queries.prune_api_requests()``.

    ``source`` and ``run_id`` attribute a request to the process or script run
    that made it; ``scripts/import_history.py`` sets both for a whole run.
    ``chat_id`` is set for requests made during a chat's turn, so deleting
    that chat's history (``history.py``) deletes them too.  When the chat has
    opted out of history, its rows carry no bodies and no query string.
    ``input_tokens``/``output_tokens`` are only populated for OpenRouter.
    """

    __tablename__ = "api_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)   # 'picnic' | 'openrouter'
    operation: Mapped[str] = mapped_column(String, nullable=False)  # client method / endpoint
    target: Mapped[str | None] = mapped_column(String, nullable=True)  # model id / resource id
    http_method: Mapped[str | None] = mapped_column(String, nullable=True)  # GET / POST / ...
    url: Mapped[str | None] = mapped_column(Text, nullable=True)  # full URL, incl. query string
    request_headers: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON, redacted
    response_headers: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON, redacted
    status: Mapped[str] = mapped_column(String, nullable=False)     # 'success' | 'error'
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_body: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    source: Mapped[str | None] = mapped_column(String, nullable=True)  # 'import_history' | ...
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Telegram chat
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_at: Mapped[str] = mapped_column(String, nullable=False)  # ISO datetime, start

    __table_args__ = (
        Index("idx_api_requests_time", "requested_at"),
        Index("idx_api_requests_provider_time", "provider", "requested_at"),
        Index("idx_api_requests_run", "run_id"),
        Index("idx_api_requests_chat_time", "chat_id", "requested_at"),
    )
