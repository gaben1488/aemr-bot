"""Актуализировать должность главы, не затрагивая изменённые настройки.

Revision ID: 0028
Revises: 0027
"""
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = (
    "Приём граждан временно исполняющим полномочия Главы Елизовского "
    "муниципального округа А.С. Гончаровым осуществляется два раза в месяц "
    "(1 и 3 среда каждого месяца) по предварительной записи. "
    "Запись на приём ведётся по номеру телефона 8 (415-31) 7-25-29."
)
_NEW = (
    "Приём граждан главой Елизовского муниципального округа "
    "Гончаровым Андреем Сергеевичем осуществляется два раза в месяц "
    "(1 и 3 среда каждого месяца) по предварительной записи. "
    "Запись на приём ведётся по номеру телефона 8 (415-31) 7-25-29."
)


def _swap(before: str, after: str) -> None:
    op.get_bind().execute(
        text("""
            UPDATE settings
               SET value = to_jsonb(CAST(:after AS text)), updated_at = now(), synced_at = NULL
             WHERE key = 'appointment_text' AND value #>> '{}' = :before
        """),
        {"before": before, "after": after},
    )


def upgrade() -> None:
    _swap(_OLD, _NEW)


def downgrade() -> None:
    _swap(_NEW, _OLD)
