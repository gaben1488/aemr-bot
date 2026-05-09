"""Backfill consent_broadcast_at для жителей, подписавшихся ДО миграции 0007.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-09

Миграция 0007 добавила колонку `users.consent_broadcast_at` — отдельное
согласие именно на рассылку. Жители, подписавшиеся через `cmd_subscribe`
или `do_subscribe` ДО миграции 0007, имеют `subscribed_broadcast=true` +
`consent_broadcast_at IS NULL` — юридически некорректное состояние:
рассылка идёт без зафиксированного факта согласия именно на эту цель.

Backfill: для всех таких жителей проставляем `consent_broadcast_at` =
`consent_pdn_at` (если он установлен — это и было согласие, частью
которого фактически шла подписка). Если consent_pdn_at NULL —
снимаем подписку вместо backfill: лучше потерять подписчика, чем
рассылать без согласия.

Параллельно пишем audit-запись `migration_consent_broadcast_backfill`
для регуляторного следа.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) У кого есть consent_pdn_at — backfill consent_broadcast_at = consent_pdn_at.
    op.execute(
        sa.text(
            """
            UPDATE users
            SET consent_broadcast_at = consent_pdn_at
            WHERE subscribed_broadcast = true
              AND consent_broadcast_at IS NULL
              AND consent_pdn_at IS NOT NULL
            """
        )
    )
    # 2) Кто подписан без consent_pdn_at — снимаем подписку (юр. безопасно).
    #    Это редкая комбинация (например, ручная вставка в БД через psql),
    #    но если есть — лучше отписать, чем рассылать без согласия.
    op.execute(
        sa.text(
            """
            UPDATE users
            SET subscribed_broadcast = false
            WHERE subscribed_broadcast = true
              AND consent_broadcast_at IS NULL
              AND consent_pdn_at IS NULL
            """
        )
    )
    # 3) Audit-запись о backfill — для регуляторного следа.
    op.execute(
        sa.text(
            """
            INSERT INTO audit_log (operator_max_user_id, action, target, details, created_at)
            VALUES (
                NULL,
                'migration_consent_broadcast_backfill',
                'all subscribed users',
                jsonb_build_object('migration', '0008'),
                now()
            )
            """
        )
    )


def downgrade() -> None:
    # Безопасный downgrade невозможен: мы не можем отличить «backfill»
    # от «реального согласия данного через мини-экран». Оставляем
    # consent_broadcast_at как есть. Удаляем только audit-запись.
    op.execute(
        sa.text(
            """
            DELETE FROM audit_log
            WHERE action = 'migration_consent_broadcast_backfill'
            """
        )
    )
