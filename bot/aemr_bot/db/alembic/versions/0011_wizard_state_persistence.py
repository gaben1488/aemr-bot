"""Persistence для wizard state'а оператора.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-11

Закрытие из senior-аудита 2026-05-11. До этой миграции in-memory dict'ы
`_op_wizards` и `_broadcast_wizards` (services/wizard_registry.py)
терялись при любом рестарте бота — `docker compose up --build`,
OOM-kill, deploy. Оператор посреди регистрации нового сотрудника или
черновика рассылки получал «🤷 мастер сброшен» без какой-либо
индикации кроме отсутствия ожидаемой подсказки.

Таблица `wizard_state` хранит активные wizards:
- `kind` — 'op' | 'broadcast' (intent/recent_replies остаются
  in-memory, они короткоживущие).
- `operator_max_user_id` — кто сейчас в wizard'е.
- `state` JSONB — то, что было в in-memory dict (step, data...).
- `expires_at` — когда state становится stale (для op-wizard 5 мин,
  broadcast-wizard 30 мин). На старте бот игнорирует expired записи.
- UNIQUE (kind, operator_max_user_id) — на одного оператора не
  больше одного wizard'а каждого вида одновременно.

GC: записи expires_at < now() игнорируются на load и удаляются
ленивым cleanup'ом в services/wizard_persist.

Не делаем wizards «реактивными» через LISTEN/NOTIFY — single-replica
boot, второй экземпляр не запускается; in-memory cache остаётся
authoritative для running-процесса, БД нужна только как durability.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wizard_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "operator_max_user_id",
            sa.BigInteger,
            nullable=False,
        ),
        sa.Column(
            "state",
            JSONB,
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "kind",
            "operator_max_user_id",
            name="uq_wizard_state_kind_operator",
        ),
    )
    op.create_index(
        "ix_wizard_state_expires_at",
        "wizard_state",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_wizard_state_expires_at", table_name="wizard_state")
    op.drop_table("wizard_state")
