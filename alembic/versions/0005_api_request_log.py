"""API request log

Adds ``api_requests``: an append-only log of calls to Picnic and OpenRouter,
written best-effort by ``api_log.py``.  A row holds provider, operation,
target, timing, outcome, error type/message, source/run id, token counts, and
the JSON request/response bodies.  Headers are never stored; credentials
(password, 2FA codes, session tokens) are redacted before a body is written,
but the table still contains prompt and order data and should be treated as
sensitive and pruned with ``db.queries.prune_api_requests()``.

Purely additive (one new table plus its indexes), so it is safe to apply to a
populated database and safe to roll back — rolling back only discards
observability data, but the next start would fail the schema check until the
code and DB agree again.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("request_body", sa.Text(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("requested_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_api_requests_time", "api_requests", ["requested_at"])
    op.create_index(
        "idx_api_requests_provider_time", "api_requests", ["provider", "requested_at"]
    )
    op.create_index("idx_api_requests_run", "api_requests", ["run_id"])


def downgrade() -> None:
    op.drop_table("api_requests")
