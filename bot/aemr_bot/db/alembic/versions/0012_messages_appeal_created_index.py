"""Композитный индекс messages(appeal_id, created_at).

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-14

Закрытие из swarm code-review (performance-агент). Relationship
`Appeal.messages` объявлен с `order_by="Message.created_at"`, и
selectinload при загрузке карточки обращения делает
`WHERE appeal_id IN (...) ORDER BY created_at`. Отдельный индекс на
`appeal_id` (миграция 0001) покрывает фильтр, но не сортировку —
на длинной переписке (followup жителя + ответы оператора) Postgres
добавляет Sort-шаг поверх index scan.

Композитный `(appeal_id, created_at)` закрывает и фильтр, и порядок
одним index scan. На текущем объёме (десятки сообщений) выигрыш
незаметен, но проект сдаётся в эксплуатацию — на горизонте года с
тысячами обращений это уже ощутимо.

Отдельный индекс `ix_messages_appeal_id` из 0001 НЕ удаляем: он
по-прежнему оптимален для `ON DELETE CASCADE` и точечных выборок
по appeal_id без сортировки, и его дроп — лишний риск ради
экономии нескольких КБ.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_messages_appeal_created",
        "messages",
        ["appeal_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_appeal_created", table_name="messages")
