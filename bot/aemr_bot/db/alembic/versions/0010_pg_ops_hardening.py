"""Postgres ops hardening: timeouts + pg_stat_statements.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-11

Закрытие из senior-аудита 2026-05-11. Три безопасных изменения для
production-надёжности БД:

1. statement_timeout = 30s (database-level).
   Любой запрос дольше 30 секунд abort-ит автоматически. Защита от
   зависшего query, который при single-replica боте полностью
   блокирует event-loop. 30s выбрано как заведомо больше типичного
   batch-запроса (рассылка по 1000 жителей: ~2-3s) и заведомо меньше
   таймаутов MAX (60s long-poll).

2. idle_in_transaction_session_timeout = 60s.
   Транзакция, забытая открытой (баг или crash на Python-стороне
   между BEGIN и COMMIT), держит row-locks. Postgres сам убьёт
   такую сессию через 60s, освободив locks. asyncpg-pool откроет
   новый коннект.

3. pg_stat_statements extension.
   Видимость в top-N медленных запросов: `select query, mean_exec_time
   from pg_stat_statements order by total_exec_time desc limit 10`.
   shared_preload_libraries включён в docker-compose.yml ДО старта
   Postgres — без этого CREATE EXTENSION пройдёт, но stats не
   запишутся. После применения миграции потребуется один рестарт
   контейнера db.

ALTER DATABASE применяется к НОВЫМ соединениям; существующий
asyncpg-pool продолжит работать со старыми настройками до рестарта
бота. Это нормально — настройки таймаутов нужны больше для cron-job
и долгосрочной защиты, не для уже идущих запросов.

postgres skill rules: lock-short-transactions (MEDIUM-HIGH),
monitor-pg-stat-statements (LOW-MEDIUM), conn-idle-timeout.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Получаем имя текущей БД, чтобы не зашивать «aemr»: в CI и в
    # alembic_check используется `aemr_alembic_check`, а в проде —
    # ${POSTGRES_DB}. ALTER DATABASE применяется только если миграция
    # запущена с правом OWNER на эту БД (бот таким правом обладает).
    op.execute(
        "ALTER DATABASE current_database() SET statement_timeout = '30s'"
    )
    op.execute(
        "ALTER DATABASE current_database() "
        "SET idle_in_transaction_session_timeout = '60s'"
    )

    # pg_stat_statements: extension создаётся, если включён preload
    # (см. docker-compose.yml command). Без preload extension всё
    # равно создастся, но stats будут пустые — разработчик увидит
    # это в /diag и поправит конфиг Postgres.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pg_stat_statements")
    op.execute(
        "ALTER DATABASE current_database() RESET idle_in_transaction_session_timeout"
    )
    op.execute("ALTER DATABASE current_database() RESET statement_timeout")
