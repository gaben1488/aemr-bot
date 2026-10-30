"""Snapshot the working calendar for newly started SLA periods.

Revision ID: 0040
Revises: 0039
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No backfill: the actual calendar of a legacy period is unknown.
    op.add_column("appeals", sa.Column("sla_schedule_at_start", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("appeals", "sla_schedule_at_start")
