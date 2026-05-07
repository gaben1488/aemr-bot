"""Индексы по внешним ключам для частых выборок и настройка autovacuum для отдельных таблиц.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-04

Два тематически связанных изменения, оба дешёвые. Заводить под каждое
отдельную миграцию нет смысла:

1. Индексы по `appeals.assigned_operator_id` и `messages.operator_id`.
   Оба столбца — внешние ключи без btree-индекса. Запрос «найти обращения
   или сообщения, которые вёл оператор X» делает полный скан, а
   ON DELETE SET NULL при деактивации строки оператора тоже идёт
   последовательно по всей дочерней таблице. На MVP-объёме это незаметно,
   но на годовом архиве (5 тыс. и более сообщений) уже мешает.

2. Настройка autovacuum для `events` и `broadcast_deliveries`. В `events`
   на каждый Update от MAX идёт один INSERT плюс ежесуточный DELETE строк
   старше 30 дней (cron events_retention). Дефолтный порог в 20% мёртвых
   строк слишком мягкий: между циклами vacuum таблица распухает.
   У `broadcast_deliveries` своя история: на каждую отправку идёт
   серия UPDATE по `delivered_at` и `error`. Снижаем scale_factor до 5%,
   чтобы autovacuum успевал за записью.
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
