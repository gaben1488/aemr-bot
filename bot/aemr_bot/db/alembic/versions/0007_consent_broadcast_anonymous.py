"""Anonymous-user pattern + consent_broadcast_at + closed_due_to_revoke.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-09

Три изменения за одну миграцию (все на тех же таблицах, выгоднее одним
upgrade чем тремя).

1. `users.consent_broadcast_at: datetime | None` — отдельное согласие
   для рассылки. Раньше подписка требовала полного согласия на ПДн
   (consent_pdn_at), что нарушало 152-ФЗ ст. 5 ч. 5 (минимизация:
   для отправки broadcast нужен только max_user_id). Теперь это
   независимая цель: для подписки достаточно тапа кнопки с понятным
   текстом — это «согласие действием» по ст. 9 ч. 1.

2. `appeals.closed_due_to_revoke: bool` — флаг «закрыто из-за отзыва
   согласия или удаления данных». Нужен, чтобы в админ-карточке
   таких обращений скрывать кнопку «🔁 Возобновить»: возобновлять
   их бессмысленно — доставка ответа всё равно отказана гардом
   `_deliver_operator_reply`.

3. Sentinel-запись «anonymous user» — техническая User-запись с
   max_user_id = -1, first_name = 'Удалено'. После полного удаления
   жителя (erase_pdn) его обращения переподвешиваются на эту запись
   через `appeals.user_id = anonymous.id`, а исходная запись жителя
   физически удаляется. Так статистика количества обращений
   сохраняется, ПДн физически уходят. max_user_id = -1 выбран как
   значение, которое не может встретиться в MAX (там user_id всегда
   положительные BigInt).

   Backfill: ON CONFLICT DO NOTHING — идемпотентно, повторные миграции
   не дублируют запись.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ANONYMOUS_MAX_USER_ID = -1


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("consent_broadcast_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "appeals",
        sa.Column(
            "closed_due_to_revoke",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Sentinel-запись для anonymous user. Проставляем consent_pdn_at
    # как NULL и first_name='Удалено' чтобы её contact_forbidden=True —
    # никаких сообщений на неё не уйдёт никогда.
    op.execute(
        sa.text(
            """
            INSERT INTO users (
                max_user_id, first_name, phone, phone_normalized,
                consent_pdn_at, consent_revoked_at, is_blocked,
                subscribed_broadcast, dialog_state, dialog_data,
                created_at, updated_at
            )
            VALUES (
                :anon_id, 'Удалено', NULL, NULL,
                NULL, NULL, true,
                false, 'idle', '{}'::jsonb,
                now(), now()
            )
            ON CONFLICT (max_user_id) DO NOTHING
            """
        ).bindparams(anon_id=ANONYMOUS_MAX_USER_ID)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM users WHERE max_user_id = :anon_id").bindparams(
            anon_id=ANONYMOUS_MAX_USER_ID
        )
    )
    op.drop_column("appeals", "closed_due_to_revoke")
    op.drop_column("users", "consent_broadcast_at")
