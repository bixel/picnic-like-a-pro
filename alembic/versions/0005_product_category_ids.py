"""Product category ids

Adds ``categories`` — Picnic's leaf (L3) categories, keyed by their L3 id,
with the L2 and L1 ids above them and the name — and ``products.category_id``
referencing it.

A product page links to its category as
``app.picnic://categories/{l1}/l2/{l2}/l3/{l3}``; until now only the L3 name
was kept, which merges same-named categories under different parents (e.g.
"Alkoholfrei" beer and wine).

Purely additive: ``products.category`` (the name) is kept, and existing rows
get ``category_id`` NULL, so the next ``scripts/import_history.py`` run looks
their category up again and fills in the ids.  Rolling back drops only the
new table and column.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-07 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK = "fk_products_category_id_categories"


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("l2_id", sa.Integer(), nullable=False),
        sa.Column("l1_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(sa.Column("category_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(_FK, "categories", ["category_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_constraint(_FK, type_="foreignkey")
        batch_op.drop_column("category_id")
    op.drop_table("categories")
