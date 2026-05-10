"""Partial-индексы под hot-path запросы.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-11

Найдено в senior-аудите 2026-05-11. Три partial-индекса под три
ежедневных/частых запроса; все три раньше делали seq scan на полной
таблице users.

1. ix_users_pending_pdn_retention — для cron-job pdn_retention_check
   (services/cron.py:_job_pdn_retention_check). Запрос отбирает
   жителей с consent_revoked_at старше 30 дней. Без индекса полный
   скан users каждые сутки. Условие partial: WHERE consent_revoked_at
   IS NOT NULL — только небольшая доля записей попадает в окно.

2. ix_users_subscribed_active — для рассылок (services/broadcasts.py:
   _eligible_filter). Compound (subscribed_broadcast, is_blocked)
   с partial WHERE subscribed_broadcast = true: сужает индекс до
   реальных подписчиков (доля ~50%, дальше is_blocked отсекает <1%).

3. ix_users_stuck_in_funnel — для funnel watchdog (services/users.py:
   find_stuck_in_funnel). Compound (dialog_state, updated_at) с
   partial WHERE is_blocked = false. Без индекса cron каждые 15
   минут scan'ит users.

postgres skill rules: query-partial-indexes (HIGH impact),
query-composite-indexes (HIGH), schema-foreign-key-indexes (HIGH).

CREATE INDEX CONCURRENTLY не используем здесь — Alembic запускает
DDL внутри транзакции, а CONCURRENTLY требует autocommit. Таблица
маленькая (< 100k строк ожидается на горизонте 2 лет), краткий lock
на CREATE INDEX приемлем — несколько миллисекунд.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) PDn-retention partial index.
    op.execute(
        "CREATE INDEX ix_users_pending_pdn_retention "
        "ON users (consent_revoked_at) "
        "WHERE consent_revoked_at IS NOT NULL"
    )

    # 2) Подписчики на рассылку — compound с partial.
    op.execute(
        "CREATE INDEX ix_users_subscribed_active "
        "ON users (subscribed_broadcast, is_blocked) "
        "WHERE subscribed_broadcast = true"
    )

    # 3) Stuck-in-funnel watchdog — compound с partial.
    op.execute(
        "CREATE INDEX ix_users_stuck_in_funnel "
        "ON users (dialog_state, updated_at) "
        "WHERE is_blocked = false"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_stuck_in_funnel")
    op.execute("DROP INDEX IF EXISTS ix_users_subscribed_active")
    op.execute("DROP INDEX IF EXISTS ix_users_pending_pdn_retention")
