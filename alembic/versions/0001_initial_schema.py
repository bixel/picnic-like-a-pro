"""Initial schema

Creates all four tables that make up the picnic-meal-planner database:
products, orders, order_items, and import_checkpoints.

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("unit_price", sa.Integer(), nullable=True),
        sa.Column("unit_quantity", sa.String(), nullable=True),
        sa.Column("image_id", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("last_seen_at", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("picnic_order_id", sa.String(), nullable=True),
        sa.Column("ordered_at", sa.String(), nullable=False),
        sa.Column("delivered_at", sa.String(), nullable=True),
        sa.Column("total_price", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("picnic_order_id"),
    )

    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "import_checkpoints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("last_delivery_id", sa.String(), nullable=True),
        sa.Column("imported_at", sa.String(), nullable=False),
        sa.Column("total_imported", sa.Integer(), server_default="0", nullable=False),
        sa.Column("finished", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("import_checkpoints")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("products")
