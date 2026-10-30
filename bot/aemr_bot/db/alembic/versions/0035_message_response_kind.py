"""Record whether a newly sent response is intermediate or final.

Revision ID: 0035
Revises: 0034
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Historical entries are intentionally left NULL: status alone cannot
    # reconstruct whether an individual response was final at the time.
    op.add_column("messages", sa.Column("response_kind", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "response_kind")
