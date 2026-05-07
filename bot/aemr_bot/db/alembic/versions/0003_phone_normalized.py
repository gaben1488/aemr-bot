"""Добавление users.phone_normalized с индексом и заполнение по существующим строкам.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-04

Зеркало users.phone из одних цифр. Поддерживается прикладным слоем
(services/users.py::_normalize_phone). Поверх лежит btree-индекс, чтобы
поиск /erase phone=... не делал полный скан users.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("phone_normalized", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_users_phone_normalized",
        "users",
        ["phone_normalized"],
    )

    # Заполнение: оставляем только цифры, у 11-значных номеров отрезаем ведущие 7 или 8.
    # В точности повторяет services/users.py::_normalize_phone.
    op.execute(
        """
        UPDATE users
        SET phone_normalized = CASE
            WHEN regexp_replace(phone, '\\D', '', 'g') ~ '^[78][0-9]{10}$'
                THEN substr(regexp_replace(phone, '\\D', '', 'g'), 2)
            ELSE regexp_replace(phone, '\\D', '', 'g')
        END
        WHERE phone IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_users_phone_normalized", table_name="users")
    op.drop_column("users", "phone_normalized")
