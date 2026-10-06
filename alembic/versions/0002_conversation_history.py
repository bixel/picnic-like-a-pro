"""Conversation history

Adds the two tables backing persisted conversation history:

- ``conversation_messages`` — one row per message, grouped into turns via
  ``turn_id``. A turn is the unit of deletion, because removing an individual
  message could orphan a ``tool_result`` block.
- ``chat_settings`` — per-chat preferences, currently just the
  ``persist_history`` opt-out flag.

Purely additive: no existing table is touched, so this is safe to apply to a
populated database and safe to roll back.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-25 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Sequential read of a chat's history.
    op.create_index(
        "idx_conversation_messages_chat",
        "conversation_messages",
        ["chat_id", "id"],
    )
    # Time-range deletes, e.g. "forget everything from day X".
    op.create_index(
        "idx_conversation_messages_time",
        "conversation_messages",
        ["chat_id", "created_at"],
    )

    op.create_table(
        "chat_settings",
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("persist_history", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    op.drop_table("chat_settings")
    op.drop_index("idx_conversation_messages_time", table_name="conversation_messages")
    op.drop_index("idx_conversation_messages_chat", table_name="conversation_messages")
    op.drop_table("conversation_messages")
