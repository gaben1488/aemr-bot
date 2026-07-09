"""Композитный индекс appeals(status, created_at).

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-09

Находка ревью при переходе SLA-просрочки на рабочее время
(services/sla.py). Горячий запрос `find_overdue_unanswered`
(services/appeals.py) и `list_unanswered`/`list_unanswered_with_messages`
фильтруют `status IN (new, in_progress)` и сортируют/сравнивают по
`created_at`:

    WHERE status IN (...) AND created_at <= threshold
    ORDER BY created_at

Отдельный индекс на `status` уже есть (`index=True` в модели, миграция
0001) и отдельный на `created_at` тоже (`index=True`, миграция 0001) —
но Postgres на двух независимых B-tree индексах чаще выбирает один из
них + bitmap-and, либо просто seq scan, если статусов в таблице
большинство (NEW/IN_PROGRESS обычно меньшинство от общего числа
appeals на горизонте месяцев). Композитный `(status, created_at)`
закрывает фильтр по статусу и сортировку/диапазон по created_at одним
index scan — тот же паттерн, что 0012 (messages) и 0009 (users).

Старые отдельные индексы на `status` и `created_at` НЕ удаляем: их
могут использовать другие запросы (например статистика по одному
только статусу без диапазона дат), а дроп — лишний риск ради
экономии нескольких КБ на небольшой таблице (тот же аргумент, что в
0012).

CREATE INDEX CONCURRENTLY не используем — Alembic гонит DDL внутри
транзакции (та же причина, что в 0009/0018); таблица `appeals` на
горизонте ожидаемого объёма (тысячи, не миллионы строк) переживает
краткий lock без проблем.

Индекс продекларирован и в db/models.py Appeal.__table_args__, чтобы
модель и БД не расходились (та же дисциплина, что 0018 для
pg_trgm-индексов users).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_appeals_status_created_at",
        "appeals",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_appeals_status_created_at", table_name="appeals")
