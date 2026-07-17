"""Убрать appeals.latitude/longitude/geo_confidence — поле оказалось лишним.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-17

Откат 0020. Координаты завели, чтобы разблокировать «пин-точный» слой
карты обращений, но решение не выдержало проверки:

1. Продуктово они ничего не давали. Житель делится геолокацией, geo.py
   распознаёт по точке населённый пункт и дом, адрес сохраняется — и
   дальше в работе участвует именно адрес: он в карточке оператора, по
   нему звонят и направляют службу. Карта строится по адресам так же.
   Потребителей у поля не нашлось ни одного: оно только записывалось,
   чистилось и выгружалось.

2. Юридически это избыточные ПДн. 152-ФЗ ст. 5 ч. 5 требует, чтобы
   объём данных соответствовал целям и не был избыточным. Координата —
   тот же адрес, только цифрами (обратная геокодировка возвращает дом),
   то есть второй экземпляр уже собранного сведения.

3. Цена проявилась сразу: поле не попало ни в один путь удаления —
   точка дома жителя переживала отзыв согласия и 5-летний ретеншн
   (чинили в #239).

Данные не теряются: в прод 0020 не уезжала, колонки заполнялись только
в тестах. Транзит координат (вложение location → geo → адрес) остаётся
как был — он в utils/attachments.py и в dialog_data, к хранению
отношения не имеет.

Downgrade: вернуть колонки (значения не восстанавливаются — их нет).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("appeals", "geo_confidence")
    op.drop_column("appeals", "longitude")
    op.drop_column("appeals", "latitude")


def downgrade() -> None:
    op.add_column("appeals", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("appeals", sa.Column("longitude", sa.Float(), nullable=True))
    op.add_column("appeals", sa.Column("geo_confidence", sa.Float(), nullable=True))
