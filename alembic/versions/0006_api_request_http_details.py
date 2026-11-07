"""HTTP details for the API request log

Adds the pieces needed to replay a logged call: the HTTP method, the full URL,
and the request/response headers.  Header values for credentials are redacted
by ``api_log.py`` before they are written; bodies remain untruncated (0005).

Purely additive.  The columns are nullable, so existing rows and any database
that already applied 0005 are unaffected.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("api_requests") as batch_op:
        batch_op.add_column(sa.Column("http_method", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("request_headers", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("response_headers", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("api_requests") as batch_op:
        batch_op.drop_column("response_headers")
        batch_op.drop_column("request_headers")
        batch_op.drop_column("url")
        batch_op.drop_column("http_method")
