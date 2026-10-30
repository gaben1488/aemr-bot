"""Persist operator reply delivery attempts before contacting MAX.

Revision ID: 0037
Revises: 0036
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("delivery_source", sa.String(16), nullable=True))
    op.create_table(
        "operator_reply_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("appeal_id", sa.Integer(), sa.ForeignKey("appeals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("operators.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_key", sa.String(255), unique=True, nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("attachments", JSONB(), nullable=False, server_default="[]"),
        sa.Column("is_final", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("max_message_id", sa.String(64), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_operator_reply_attempts_appeal_id", "operator_reply_attempts", ["appeal_id"])
    op.create_index("ix_operator_reply_attempts_state", "operator_reply_attempts", ["state"])


def downgrade() -> None:
    op.drop_index("ix_operator_reply_attempts_state", table_name="operator_reply_attempts")
    op.drop_index("ix_operator_reply_attempts_appeal_id", table_name="operator_reply_attempts")
    op.drop_table("operator_reply_attempts")
    op.drop_column("messages", "delivery_source")
