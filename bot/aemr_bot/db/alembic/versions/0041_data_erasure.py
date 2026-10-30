"""Inventory external resident copies and retain restore tombstones.

Revision ID: 0041
Revises: 0040
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("appeals", sa.Column("content_erased_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("appeals", sa.Column("work_started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table("erasure_tombstones",
        sa.Column("subject_digest", sa.String(64), primary_key=True),
        sa.Column("erased_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("backups_state", sa.String(16), server_default="pending", nullable=False),
        sa.Column("last_error", sa.String(64)))
    op.create_table("resident_data_copies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("external_key", sa.String(36), nullable=False, unique=True),
        sa.Column("subjects", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("appeal_id", sa.Integer()),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(255)),
        sa.Column("state", sa.String(16), server_default="reserved", nullable=False),
        sa.Column("erase_requested", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("last_error", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("message_id", name="uq_resident_data_copy_mid"))
    op.create_index("ix_resident_data_copies_appeal_id", "resident_data_copies", ["appeal_id"])
    op.create_index("ix_resident_data_copies_state", "resident_data_copies", ["state"])


def downgrade() -> None:
    op.drop_table("resident_data_copies")
    op.drop_table("erasure_tombstones")
    op.drop_column("appeals", "work_started_at")
    op.drop_column("appeals", "content_erased_at")
