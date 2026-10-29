"""Remember the internal first-response target for new appeal periods.

Revision ID: 0038
Revises: 0037
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing periods stay NULL: their original configuration is unknown.
    op.add_column("appeals", sa.Column("sla_hours_at_start", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("appeals", "sla_hours_at_start")
