"""Link new repeated appeals without changing the resident's text.

Revision ID: 0034
Revises: 0033
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("appeals", sa.Column("related_appeal_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_appeals_related_appeal_id", "appeals", "appeals",
                          ["related_appeal_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_appeals_related_appeal_id", "appeals", type_="foreignkey")
    op.drop_column("appeals", "related_appeal_id")
