"""broadcast_templates: счётчик применений + дата последнего применения.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-21

PR template-editor-upgrade: для гигиены списка шаблонов добавляем поля
- `use_count INTEGER NOT NULL DEFAULT 0` — сколько раз шаблон
  применили как рассылку (incrementится в `_apply`);
- `last_used_at TIMESTAMPTZ NULL` — момент последнего применения.

Используются в карточке шаблона («Применён 12 раз, последний раз
2 мая») и при потенциальной будущей сортировке по «горячим».

Downgrade: дроп колонок. Не задевает существующие записи (default 0 /
NULL покрывают backfill).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broadcast_templates",
        sa.Column(
            "use_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "broadcast_templates",
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("broadcast_templates", "last_used_at")
    op.drop_column("broadcast_templates", "use_count")
