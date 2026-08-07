"""broadcast_deliveries: UNIQUE(broadcast_id, user_id) — идемпотентность доставок.

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-30

Flush-буфер результатов рассылки (handlers/broadcast.py::_flush_pending)
best-effort: при сбое сети/БД буфер НЕ очищается и уходит повторно со
следующей пачкой. Если часть строк успела закоммититься до обрыва,
повтор записывал пару (broadcast_id, user_id) второй раз — и
count_delivery_results задваивал счётчики «доставлено/ошибки», по
которым оператор решает, перезапускать ли рассылку.

Ограничение UNIQUE(broadcast_id, user_id) делает пару физически
однократной; код записи (services/broadcasts.record_delivery /
record_deliveries) переведён на INSERT .. ON CONFLICT DO NOTHING.

Перед созданием ограничения удаляем уже накопленные дубли, оставляя
самую раннюю строку пары (min(id) — первая записанная попытка,
последующие — ретраи того же результата).

Downgrade: drop constraint (удалённые дубли не восстанавливаются —
они и были ошибочными повторами).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "uq_broadcast_deliveries_broadcast_user"


def upgrade() -> None:
    # Self-join вместо оконной функции: короче и использует PK-индекс.
    # Для каждой пары выживает строка с минимальным id.
    op.execute(
        """
        DELETE FROM broadcast_deliveries bd
        USING broadcast_deliveries keep
        WHERE keep.broadcast_id = bd.broadcast_id
          AND keep.user_id = bd.user_id
          AND keep.id < bd.id
        """
    )
    op.create_unique_constraint(
        _CONSTRAINT,
        "broadcast_deliveries",
        ["broadcast_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "broadcast_deliveries", type_="unique")
