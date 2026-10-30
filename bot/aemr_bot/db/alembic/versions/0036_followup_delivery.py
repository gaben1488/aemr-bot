"""Remember whether the admin notification for a follow-up was attempted.

Revision ID: 0036
Revises: 0035
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing messages have unknown prior delivery; do not replay them.
    op.add_column("messages", sa.Column("admin_notice_state", sa.String(16), nullable=True))
    op.create_index("ix_messages_admin_notice_state", "messages", ["admin_notice_state"])


def downgrade() -> None:
    op.drop_index("ix_messages_admin_notice_state", table_name="messages")
    op.drop_column("messages", "admin_notice_state")
