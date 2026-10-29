"""Track the beginning of a new response period after reopening.

Revision ID: 0032
Revises: 0031
"""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("appeals", sa.Column("sla_anchor_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("appeals", sa.Column("sla_history", postgresql.JSONB(), nullable=False,
                                       server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.drop_column("appeals", "sla_history")
    op.drop_column("appeals", "sla_anchor_at")
