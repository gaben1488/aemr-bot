"""Image attachments в рассылках.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-21

Добавляет в таблицу `broadcasts` колонку `attachments JSONB NOT NULL
DEFAULT '[]'` — для хранения сериализованных image-attachment'ов
рассылки между confirm'ом мастера и фоновой отправкой подписчикам.

Формат значения — тот же list[dict], что в Appeal.attachments и
Message.attachments: каждый элемент — словарь с ключами `type`,
`payload` и т.п., полученный через
utils/attachments.collect_attachments из сообщения оператора. На
исход в send-loop восстанавливается в pydantic-объекты через
utils/attachments.deserialize_for_relay.

Текстовые рассылки (без картинок) хранят пустой список — обратная
совместимость со старыми broadcast'ами. NOT NULL с server_default='[]'
гарантирует, что старые row'ы после миграции имеют валидное значение.

Downgrade: дроп колонки. Безопасно — поле не индексируется и не имеет
внешних ссылок; восстанавливается из заново созданных рассылок.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "broadcasts",
        sa.Column(
            "attachments",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("broadcasts", "attachments")
