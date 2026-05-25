"""appeals.last_admin_card_mid — указатель на ПОСЛЕДНЮЮ event-карточку.

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-25

DDD pivot (см. брейншторм продуктового аудита 2026-05-25):

`Appeal.admin_message_id` оставляем как «mid первой опубликованной
карточки» (используется для reply-link при relay вложений к
finalize-карточке). НЕ редактируется после finalize.

`Appeal.last_admin_card_mid` — новое поле. Mid ПОСЛЕДНЕЙ event-
карточки этого обращения в админ-чате. Обновляется при каждом render
(finalize, followup, reply, status-change). Используется для:

1. Stale-detection: callback.message.mid сверяется с last_admin_card_mid;
   если оператор тапнул на старой карточке вверху чата — ack +
   render новой event-карточки в самый низ.

2. Точка свайп-reply: оператор свайпом отвечает на ПОСЛЕДНЮЮ карточку.

Все admin-карточки иммутабельные event-records — каждое изменение
статуса публикует новую, старые остаются в чате как audit-trail.

Downgrade: drop column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "appeals",
        sa.Column("last_admin_card_mid", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appeals", "last_admin_card_mid")
