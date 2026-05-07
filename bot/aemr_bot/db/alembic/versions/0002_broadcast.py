"""подписка на рассылку и связанные таблицы

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-04

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "subscribed_broadcast",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "created_by_operator_id",
            sa.Integer,
            sa.ForeignKey("operators.id", ondelete="SET NULL"),
        ),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("subscriber_count_at_start", sa.Integer, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("delivered_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("admin_message_id", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_broadcasts_status", "broadcasts", ["status"])
    op.create_index("ix_broadcasts_created_at", "broadcasts", ["created_at"])

    op.create_table(
        "broadcast_deliveries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "broadcast_id",
            sa.Integer,
            sa.ForeignKey("broadcasts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text),
    )
    op.create_index(
        "ix_broadcast_deliveries_broadcast_id",
        "broadcast_deliveries",
        ["broadcast_id"],
    )
    op.create_index(
        "ix_broadcast_deliveries_user_id",
        "broadcast_deliveries",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_broadcast_deliveries_user_id", table_name="broadcast_deliveries")
    op.drop_index(
        "ix_broadcast_deliveries_broadcast_id",
        table_name="broadcast_deliveries",
    )
    op.drop_table("broadcast_deliveries")

    op.drop_index("ix_broadcasts_created_at", table_name="broadcasts")
    op.drop_index("ix_broadcasts_status", table_name="broadcasts")
    op.drop_table("broadcasts")

    op.drop_column("users", "subscribed_broadcast")
