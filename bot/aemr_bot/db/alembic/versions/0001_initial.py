"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-29

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("max_user_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("first_name", sa.String(120)),
        sa.Column("phone", sa.String(32)),
        sa.Column("consent_pdn_at", sa.DateTime(timezone=True)),
        sa.Column("is_blocked", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("dialog_state", sa.String(32), nullable=False, server_default="idle"),
        sa.Column("dialog_data", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_users_max_user_id", "users", ["max_user_id"], unique=True)

    op.create_table(
        "operators",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("max_user_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_operators_max_user_id", "operators", ["max_user_id"], unique=True)

    op.create_table(
        "appeals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="new"),
        sa.Column("address", sa.String(500)),
        sa.Column("topic", sa.String(120)),
        sa.Column("summary", sa.Text),
        sa.Column("attachments", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("admin_message_id", sa.String(64)),
        sa.Column("assigned_operator_id", sa.Integer, sa.ForeignKey("operators.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_appeals_user_id", "appeals", ["user_id"])
    op.create_index("ix_appeals_status", "appeals", ["status"])
    op.create_index("ix_appeals_created_at", "appeals", ["created_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("appeal_id", sa.Integer, sa.ForeignKey("appeals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("text", sa.Text),
        sa.Column("attachments", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("max_message_id", sa.String(64)),
        sa.Column("operator_id", sa.Integer, sa.ForeignKey("operators.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_messages_appeal_id", "messages", ["appeal_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("update_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_events_idempotency_key", "events", ["idempotency_key"], unique=True)
    op.create_index("ix_events_received_at", "events", ["received_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("operator_max_user_id", sa.BigInteger),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(255)),
        sa.Column("details", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", postgresql.JSONB),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_events_received_at", table_name="events")
    op.drop_index("ix_events_idempotency_key", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_messages_appeal_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_appeals_created_at", table_name="appeals")
    op.drop_index("ix_appeals_status", table_name="appeals")
    op.drop_index("ix_appeals_user_id", table_name="appeals")
    op.drop_table("appeals")
    op.drop_index("ix_operators_max_user_id", table_name="operators")
    op.drop_table("operators")
    op.drop_index("ix_users_max_user_id", table_name="users")
    op.drop_table("users")
