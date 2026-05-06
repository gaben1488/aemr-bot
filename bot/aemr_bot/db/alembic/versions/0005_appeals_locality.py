"""Add appeals.locality column for population-point selection step.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-05

В Елизовском муниципальном районе несколько поселений: одно
городское (Елизовское), плюс несколько городских и сельских (Вулканное,
Корякское, Начикинское, Николаевское, Новоавачинское, Новолесновское,
Паратунское, Пионерское, Раздольненское). Раньше всё писалось в одно
поле «адрес». Координаторам это создавало проблему при распределении
обращений между территориальными управлениями.

Колонка `appeals.locality` хранит выбор жителя на отдельном шаге
анкеты. Старые обращения остаются с NULL — это нормально, в выгрузках
они показываются как «не указано».
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "appeals",
        sa.Column("locality", sa.String(120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appeals", "locality")
