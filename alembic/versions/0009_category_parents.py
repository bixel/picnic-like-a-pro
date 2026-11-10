"""Names of the L1 and L2 categories

Adds ``category_parents`` — the names of Picnic's L1 and L2 categories, keyed
by level and id — so a category's whole path can be read.  ``categories.name``
stays the only copy of an L3 name.

Also fills ``categories.l1_id`` where it is NULL: L3s stored from an L2 page,
rather than through a product's link, were saved without one.  Every L3 of an
L2 shares its L1, so the id is taken from a sibling.

Rolling back drops the table and keeps the filled ids, which are correct.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-10 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "category_parents",
        sa.Column("level", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("level", "id"),
    )
    op.execute(
        "UPDATE categories SET l1_id = "
        "(SELECT max(c.l1_id) FROM categories c WHERE c.l2_id = categories.l2_id) "
        "WHERE l1_id IS NULL"
    )


def downgrade() -> None:
    op.drop_table("category_parents")
