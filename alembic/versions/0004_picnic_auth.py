"""Picnic session token

Adds ``picnic_auth``: a single row holding the Picnic session token, so the
bot, MCP server and scripts reuse one login instead of each logging in with
the password — which, on an account with 2FA, would need a new code each time.

Purely additive (one new table), so this is safe to apply to a populated
database and safe to roll back; rolling back only forgets the session, and
the next start logs in again.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "picnic_auth",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("auth_token", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("picnic_auth")
