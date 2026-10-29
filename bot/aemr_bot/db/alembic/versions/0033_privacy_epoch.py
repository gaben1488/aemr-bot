"""Invalidate exports built before a completed personal-data erasure.

Revision ID: 0033
Revises: 0032
"""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table("privacy_epoch", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"))
    op.execute("INSERT INTO privacy_epoch (id, generation) VALUES (1, 0)")


def downgrade() -> None:
    op.drop_table("privacy_epoch")
