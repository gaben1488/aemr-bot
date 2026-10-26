"""Durable inbound receipts and explicit initial-card delivery state."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("appeals", sa.Column("initial_card_state", sa.String(16), nullable=False, server_default="pending"))
    op.add_column("appeals", sa.Column("initial_card_notified", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.execute("UPDATE appeals SET initial_card_state = 'published' WHERE admin_message_id IS NOT NULL")
    op.create_table(
        "inbox_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_key", sa.String(64), nullable=False, unique=True),
        sa.Column("update_type", sa.String(64), nullable=False),
        sa.Column("max_user_id", sa.BigInteger(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    for name in ("max_user_id", "state", "received_at"):
        op.create_index(f"ix_inbox_events_{name}", "inbox_events", [name])


def downgrade() -> None:
    op.drop_table("inbox_events")
    op.drop_column("appeals", "initial_card_notified")
    op.drop_column("appeals", "initial_card_state")
