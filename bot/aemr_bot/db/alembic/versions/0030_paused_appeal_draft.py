"""Preserve an unfinished resident workflow while navigating menus.

Revision ID: 0030
Revises: 0029
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("appeal_draft", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "appeal_draft")
