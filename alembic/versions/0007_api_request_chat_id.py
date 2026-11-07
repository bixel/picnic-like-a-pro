"""Chat attribution for the API request log

Adds ``api_requests.chat_id``: the Telegram chat whose turn made the request,
or NULL for requests made outside a chat (import script, MCP server, login).
It lets ``history.py`` delete a chat's logged requests together with the
conversation they belong to (``/forget``, ``delete_turns``, ``delete_day``),
so the log cannot outlive a deletion the user asked for.

Purely additive: one nullable column and one index.  Existing rows keep
``chat_id`` NULL.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("api_requests") as batch_op:
        batch_op.add_column(sa.Column("chat_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            "idx_api_requests_chat_time", ["chat_id", "requested_at"]
        )


def downgrade() -> None:
    with op.batch_alter_table("api_requests") as batch_op:
        batch_op.drop_index("idx_api_requests_chat_time")
        batch_op.drop_column("chat_id")
