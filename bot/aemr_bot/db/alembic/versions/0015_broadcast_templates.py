"""Таблица broadcast_templates — пул типовых рассылок.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-21

Добавляет таблицу `broadcast_templates` для хранения шаблонов
рассылок: name (человекочитаемое имя, уникальное), text, attachments
(image-вложения в том же формате, что Broadcast.attachments), автор
и timestamp'ы. Soft-delete через archived_at — удалённые шаблоны
остаются для аудита, в выборку «активных» не попадают.

Downgrade: дроп таблицы (cascade — никакая другая таблица на неё не
ссылается, FK к operators имеет ON DELETE SET NULL).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "broadcast_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "attachments",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_by_operator_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_operator_id"],
            ["operators.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_broadcast_template_name"),
    )
    op.create_index(
        "ix_broadcast_templates_archived_at",
        "broadcast_templates",
        ["archived_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_broadcast_templates_archived_at",
        table_name="broadcast_templates",
    )
    op.drop_table("broadcast_templates")
