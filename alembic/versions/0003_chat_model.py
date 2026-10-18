"""Per-chat LLM model

Adds ``chat_settings.model``: the OpenRouter model id a conversation uses.
``NULL`` means "follow the deployment default" (``LLM_MODEL``), so every
existing row keeps working unchanged.

Purely additive (one nullable column), so this is safe to apply to a populated
database and safe to roll back. ``batch_alter_table`` is used because SQLite
cannot drop a column in place on downgrade.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("chat_settings") as batch_op:
        batch_op.add_column(sa.Column("model", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("chat_settings") as batch_op:
        batch_op.drop_column("model")
