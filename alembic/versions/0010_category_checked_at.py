"""When category names were last confirmed by Picnic

Adds ``checked_at`` to ``categories`` and ``category_parents`` so names can be
looked up again: a stale name is refreshed, and a lookup that found no name is
retried after a while instead of on every run.  ``category_parents.name``
becomes nullable — a row with no name records a lookup that found none.

Existing rows get NULL, which counts as "never checked": they are refreshed
gradually by the import's per-run limit.

Rolling back drops the columns and the unnamed rows.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-11 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("categories") as batch:
        batch.add_column(sa.Column("checked_at", sa.String(), nullable=True))
    with op.batch_alter_table("category_parents") as batch:
        batch.alter_column("name", existing_type=sa.String(), nullable=True)
        batch.add_column(sa.Column("checked_at", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM category_parents WHERE name IS NULL")
    with op.batch_alter_table("category_parents") as batch:
        batch.drop_column("checked_at")
        batch.alter_column("name", existing_type=sa.String(), nullable=False)
    with op.batch_alter_table("categories") as batch:
        batch.drop_column("checked_at")
