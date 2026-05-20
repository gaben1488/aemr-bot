"""Repo sync metadata + commit author settings.

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-20

Добавляет в таблицу `settings` колонку `synced_at` — отметку времени
последней успешной выгрузки этого ключа в репозиторий через
services/repo_sync. Это поле читает меню «⚙️ Настройки бота», чтобы
показывать «есть N несинхронизированных изменений».

Логика:
- При set_value(key, value) поле обнуляется → ключ считается «грязным»
  (изменён в БД, но в репо ещё не уехал).
- После успешного создания PR через services/repo_sync синхронизация
  ставит synced_at = now() для всех ключей, попавших в коммит.
- В меню сравнение updated_at vs synced_at даёт счётчик «dirty» ключей.

Параллельно гарантируем, что в DEFAULTS settings_store есть записи для
commit_author_name и commit_author_email (создаются ленивно через
set_value при первом обращении пользователя к меню автора, миграция
ничего не вставляет — это конфиг, а не схема).

Downgrade: дроп колонки. Безопасно — синхронизация всё равно
переинициализирует synced_at при следующем PR.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "settings",
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("settings", "synced_at")
