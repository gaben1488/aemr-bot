"""Keep the exact consent displayed for future acceptances.

Revision ID: 0029
Revises: 0028
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column(
        "consent_records", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ))


def downgrade() -> None:
    # As with any destructive schema rollback, export evidence beforehand.
    op.drop_column("users", "consent_records")
