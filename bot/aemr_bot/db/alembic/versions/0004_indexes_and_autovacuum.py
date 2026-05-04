"""Add FK indexes for hot lookups + per-table autovacuum tuning.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-04

Two thematically related changes that are dirt-cheap and don't deserve
separate migrations:

1. Indexes on `appeals.assigned_operator_id` and `messages.operator_id`.
   Both are FKs without btree indexes. "Find appeals/messages handled
   by operator X" sequential-scans, and ON DELETE SET NULL also scans
   the entire child table when an operator row is deactivated. On the
   MVP scale this is invisible; on a full-year archive (5k+ messages)
   it starts mattering.

2. Per-table autovacuum tuning for `events` and `broadcast_deliveries`.
   `events` gets one INSERT per Update from MAX plus a daily DELETE of
   30+ day rows (events_retention cron). The default 20%-dead trigger
   for autovacuum is too lax — bloat accumulates between vacuum cycles.
   `broadcast_deliveries` gets a flurry of UPDATE on `delivered_at` /
   `error` per send, same story. Drop scale_factor to 5% so autovacuum
   chases the writes.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_appeals_assigned_operator_id",
        "appeals",
        ["assigned_operator_id"],
    )
    op.create_index(
        "ix_messages_operator_id",
        "messages",
        ["operator_id"],
    )

    op.execute(
        "ALTER TABLE events SET ("
        "  autovacuum_vacuum_scale_factor = 0.05,"
        "  autovacuum_analyze_scale_factor = 0.05"
        ")"
    )
    op.execute(
        "ALTER TABLE broadcast_deliveries SET ("
        "  autovacuum_vacuum_scale_factor = 0.05,"
        "  autovacuum_analyze_scale_factor = 0.05"
        ")"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE broadcast_deliveries RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor)")
    op.execute("ALTER TABLE events RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor)")
    op.drop_index("ix_messages_operator_id", table_name="messages")
    op.drop_index("ix_appeals_assigned_operator_id", table_name="appeals")
