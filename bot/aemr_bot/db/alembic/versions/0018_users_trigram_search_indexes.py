"""Trigram GIN-индексы под поиск жителя (search_audience).

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-02

Закрытие perf-находки (Волна 2). services/users.py::search_audience
ищет жителя для админ-меню «Аудитория» через
`first_name ILIKE '%query%'` и `phone_normalized ILIKE '%digits%'`.
ILIKE с ведущим `%` НЕ может использовать обычный B-tree индекс
(`ix_users_phone_normalized` из 0003 покрывает только префиксный и
exact-match поиск), поэтому каждый поиск — seq scan по users.

pg_trgm + GIN с `gin_trgm_ops` индексирует триграммы строки и
ускоряет именно подстрочный ILIKE/LIKE `%...%`. Два индекса:

  - ix_users_first_name_trgm   — first_name (поиск по имени);
  - ix_users_phone_normalized_trgm — phone_normalized (частичный
    телефон).

Объём users мал (на горизонте 2 лет < 100k строк), поэтому обычный
CREATE INDEX (не CONCURRENTLY) приемлем: краткий lock на несколько
миллисекунд. CONCURRENTLY здесь не используем сознательно — он требует
autocommit, а Alembic гонит DDL внутри транзакции (та же причина, что
в 0009). На маленькой таблице выигрыш CONCURRENTLY не стоит возни с
isolation_level.

services/users.py НЕ меняется: ILIKE-запрос уже корректен, ему нужен
только индекс. Оба индекса продекларированы в db/models.py
User.__table_args__ (postgresql_using='gin' + postgresql_ops), чтобы
`alembic check` не видел drift между моделью и БД.

CREATE EXTENSION IF NOT EXISTS / CREATE INDEX IF NOT EXISTS —
идемпотентны: повторный upgrade на БД, где индексы уже есть, не падает.

Downgrade: дропаем оба индекса. Extension pg_trgm НЕ дропаем — её
могут использовать другие объекты, а на пустой роли DROP EXTENSION
безвреден лишь при отсутствии зависимостей; оставляем расширение на
месте (дешёвое, безопасное «лишнее»). Это сознательно отличается от
0010 (там pg_stat_statements дропается), потому что pg_trgm может стать
зависимостью будущих индексов.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pg_trgm даёт operator class gin_trgm_ops для триграммного GIN.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_first_name_trgm "
        "ON users USING gin (first_name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_phone_normalized_trgm "
        "ON users USING gin (phone_normalized gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_phone_normalized_trgm")
    op.execute("DROP INDEX IF EXISTS ix_users_first_name_trgm")
