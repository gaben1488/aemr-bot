"""Version settings changes to reject stale buttons even after ABA changes.

Revision ID: 0031
Revises: 0030
"""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("settings", sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"))


def downgrade() -> None:
    op.drop_column("settings", "revision")
