"""users.consent_pdn_text_sha256 — доказуемость согласия.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-20

152-ФЗ ст. 9 ч. 3 возлагает на оператора обязанность ДОКАЗАТЬ, что
согласие было получено. Раньше в базе хранилась только отметка времени
`consent_pdn_at`, а текст согласия (`consent_text`) редактируется через
админ-UI на лету. После любой правки становилось невозможно доказать, на
какую именно редакцию житель нажал «Согласен»: время есть, а под чем
подписано — неизвестно. Для согласия как ЕДИНСТВЕННОГО основания
обработки это существенный дефект.

Добавляем SHA-256 действующей на момент согласия редакции текста. Хеш,
а не сам текст: редакций немного, они лежат в истории настроек и в
архиве политики, а хеш даёт компактную несомненную привязку «этот
житель согласился именно на эту редакцию». При споре предъявляется
редакция с совпадающим хешем.

Nullable: у согласий, данных до этой миграции, хеша нет — так и
остаётся NULL (задним числом его не восстановить, и подделывать нельзя).

Downgrade: drop column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("consent_pdn_text_sha256", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "consent_pdn_text_sha256")
