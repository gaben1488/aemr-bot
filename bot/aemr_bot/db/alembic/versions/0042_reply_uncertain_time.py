"""Record the upper bound of manually confirmed reply delivery time.

Revision ID: 0042
Revises: 0041
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("delivery_uncertain_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("operator_reply_attempts", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM messages WHERE delivery_uncertain_until IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM operator_reply_attempts WHERE last_activity_at IS NOT NULL)"
    )).scalar():
        raise RuntimeError("Cannot drop uncertain delivery bounds while evidence exists")
    op.drop_column("operator_reply_attempts", "last_activity_at")
    op.drop_column("messages", "delivery_uncertain_until")
