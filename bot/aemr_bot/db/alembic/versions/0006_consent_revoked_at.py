"""Add users.consent_revoked_at column.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-08

Колонка нужна, чтобы отделить «согласие никогда не давалось» (NULL и
там, и там) от «согласие давалось, потом было явно отозвано» (consent_pdn_at
обнулён, consent_revoked_at = когда отозвал).

Зачем разделять: после отзыва бот не принимает новые обращения без
нового согласия, но по уже принятому открытому обращению оператор может
дать финальный ответ через бот. Без точки отзыва эту границу установить
невозможно.

Колонка nullable: у уже существующих жителей значение NULL, и логика
бота интерпретирует это как «не отзывал». Старые записи, у которых
consent_pdn_at тоже NULL, остаются в состоянии «никогда не давал
согласия», и поведение для них не меняется.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("consent_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "consent_revoked_at")
