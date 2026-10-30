"""Durable multipart operator composition.

Revision ID: 0039
Revises: 0038
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("operator_reply_attempts", sa.Column("delivery_parts", JSONB(), nullable=False, server_default="[]"))
    op.create_table(
        "operator_reply_drafts",
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("operators.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("appeal_id", sa.Integer(), sa.ForeignKey("appeals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(32), nullable=False, unique=True),
        sa.Column("is_final", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="editing"),
        sa.Column("data", JSONB(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_operator_reply_drafts_appeal_id", "operator_reply_drafts", ["appeal_id"])


def downgrade():
    op.drop_table("operator_reply_drafts")
    op.drop_column("operator_reply_attempts", "delivery_parts")
