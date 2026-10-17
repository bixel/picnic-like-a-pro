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


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    unit_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_quantity: Mapped[str | None] = mapped_column(String, nullable=True)
    image_id: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    last_seen_at: Mapped[str | None] = mapped_column(String, nullable=True)  # ISO datetime

    order_items: Mapped[list[OrderItem]] = relationship(back_populates="product")


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
    """Per-chat preferences. Currently just the history-persistence opt-out."""

    __tablename__ = "chat_settings"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 0 = this chat opted out of having its conversation stored. server_default
    # keeps metadata.create_all() and the Alembic migration in exact agreement.
    persist_history: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    updated_at: Mapped[str] = mapped_column(String, nullable=False)  # ISO datetime
